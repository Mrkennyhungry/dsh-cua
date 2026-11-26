"""Bounded semantic snapshots, stable references and owner-scoped image resources."""
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import threading
import time
import uuid

import cv2

from .targets import RuntimeFault
from .worker import ApartmentWorker

logger = logging.getLogger(__name__)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def integrity_evidence(pid):
    """Compare Windows integrity RIDs when both process tokens are readable."""
    try:
        import win32api
        import win32con
        import win32security
        def level(process):
            token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
            try:
                sid, _ = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)
                return sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)
            finally:
                token.Close()
        process = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        try:
            target_level = level(process)
            kernel_level = level(win32api.GetCurrentProcess())
        finally:
            process.Close()
        result = {'kernel_integrity': kernel_level, 'target_integrity': target_level,
                  'uipi_risk': target_level > kernel_level}
        if result['uipi_risk']:
            result['advice'] = 'Input may be blocked by UIPI. Restart the kernel using the configured elevated task before retrying.'
        return result
    except Exception:
        return {'uipi_risk': None}


@dataclass
class Element:
    ref: str
    source: str
    target_id: str
    document_id: str | None
    semantic: dict
    locator: dict = field(repr=False)
    rect: tuple | None = None

    def public(self, frame=None):
        value = {'ref': self.ref, 'source': self.source, **self.semantic}
        for key, limit in (('name', 240), ('value', 1000)):
            if isinstance(value.get(key), str) and len(value[key]) > limit:
                value[key] = value[key][:limit]
                value[key + '_truncated'] = True
        if 'options' in value:
            options = value['options']
            value['options'] = [option[:120] for option in options[:20]]
            value['options_truncated'] = value.get('options_truncated', False) or len(options) > 20
        if frame is not None and self.rect is not None:
            x, y, width, height = self.rect
            left, top = frame.geometry.from_screen(x, y)
            right, bottom = frame.geometry.from_screen(x + width, y + height)
            left, top = max(0, left), max(0, top)
            right, bottom = min(frame.geometry.image_size[0], right), min(frame.geometry.image_size[1], bottom)
            if right > left and bottom > top:
                value['bounds'] = {'frame_id': frame.frame_id, 'x': round(left, 2), 'y': round(top, 2),
                                   'width': round(right - left, 2), 'height': round(bottom - top, 2)}
            else:
                value['offscreen'] = True
        return value


@dataclass
class Observation:
    observation_id: str
    owner: str
    target_id: str
    revision: int
    captured_at: str
    elements: list
    text: str
    metadata: dict
    frames: list = field(default_factory=list)
    images: dict = field(default_factory=dict, repr=False)
    created: float = field(default_factory=time.monotonic)


class ObservationService:
    def __init__(self, runtime, *, limit=64, ttl=300, image_budget=32 * 1024 * 1024):
        self.runtime = runtime
        self.limit = limit
        self.ttl = ttl
        self.image_budget = image_budget
        self._snapshots = OrderedDict()
        self._last_views = {}
        self._uia_elements = OrderedDict()
        self._lock = threading.RLock()
        self.uia = ApartmentWorker('dsh-uia', cleanup=self._uia_elements.clear)
        self._pending_uia_owners = set()

    def _drop(self, observation_id):
        observation = self._snapshots.pop(observation_id)
        for frame in observation.frames:
            self.runtime.frames.discard(frame.frame_id)

    def _prune(self):
        cutoff = time.monotonic() - self.ttl
        for key in [key for key, value in self._snapshots.items() if value.created < cutoff]:
            self._drop(key)
        total = sum(len(data) for value in self._snapshots.values() for data in value.images.values())
        while self._snapshots and (len(self._snapshots) > self.limit or total > self.image_budget):
            oldest = next(iter(self._snapshots))
            total -= sum(map(len, self._snapshots[oldest].images.values()))
            self._drop(oldest)

    def get(self, owner, observation_id, target_id=None):
        with self._lock:
            self._prune()
            value = self._snapshots.get(observation_id)
            if value is None or value.owner != owner:
                raise RuntimeFault('OBSERVATION_EXPIRED', 'Capture a new observation for this session')
            if target_id and value.target_id != target_id:
                try:
                    self.runtime.resolve_target(owner, value.target_id)
                except RuntimeFault as error:
                    if error.code == 'TARGET_CLOSED':
                        raise RuntimeFault('TARGET_STALE', 'Observation belongs to a closed target. Observe the current target') from error
                    raise
                raise RuntimeFault('TARGET_MISMATCH', 'Observation belongs to another target')
            return value

    def image(self, owner, frame_id):
        with self._lock:
            self._prune()
            for observation in self._snapshots.values():
                if observation.owner == owner and frame_id in observation.images:
                    return observation.images[frame_id]
        raise RuntimeFault('FRAME_EXPIRED', 'Image resource has expired')

    def capture_frame(self, owner, target_id, *, screen_point=None):
        """Store an immediate frame without waiting for semantic providers."""
        options = {'screen_point': screen_point} if screen_point is not None else {}
        pixels, frame = self.runtime.capture_target(owner, target_id, **options)
        ok, encoded = cv2.imencode('.jpg', pixels, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok or encoded.nbytes > 4 * 1024 * 1024:
            self.runtime.frames.discard(frame.frame_id)
            raise RuntimeFault('IMAGE_TOO_LARGE', 'Could not store the captured frame')
        observation = Observation('o_' + uuid.uuid4().hex, owner, target_id, frame.revision,
                                  timestamp(), [], '', {}, [frame], {frame.frame_id: encoded.tobytes()})
        with self._lock:
            self._snapshots[observation.observation_id] = observation
            self._prune()
        return {**frame.summary(), 'observation_id': observation.observation_id, 'captured_at': observation.captured_at}

    def popup_candidates(self, owner, before_info, frame_info, region):
        """Compare OCR from the same bounded area before and after a click."""
        import numpy as np
        observation = self.get(owner, frame_info['observation_id'])
        frame = observation.frames[0]
        before = self.get(owner, before_info['observation_id'])
        if not before.frames or before.frames[0].geometry != frame.geometry:
            raise RuntimeFault('FRAME_CHANGED', 'Cannot compare popup candidates across different frame geometry')
        pixels = cv2.imdecode(np.frombuffer(self.image(owner, frame.frame_id), np.uint8), cv2.IMREAD_COLOR)
        previous = cv2.imdecode(np.frombuffer(self.image(owner, before.frames[0].frame_id), np.uint8), cv2.IMREAD_COLOR)
        if pixels is None or previous is None or pixels.shape != previous.shape:
            raise RuntimeFault('FRAME_EXPIRED', 'Post-click image could not be decoded')
        x, y, width, height = (region[key] for key in ('x', 'y', 'width', 'height'))
        def compare():
            baseline = self.runtime.perceive(previous[y:y + height, x:x + width], 'ocr')
            current = self.runtime.perceive(pixels[y:y + height, x:x + width], 'ocr')
            # Treat a matching text box near its former location as pre-existing.
            # OCR differences remain candidates, not proof that a popup opened.
            def existed(node):
                box = node['bbox']
                for prior in baseline:
                    old = prior['bbox']
                    if (str(prior.get('text', '')).strip() == str(node.get('text', '')).strip()
                            and max(box[0], old[0]) < min(box[2], old[2])
                            and max(box[1], old[1]) < min(box[3], old[3])):
                        return True
                return False
            return [node for node in current if not existed(node)]
        nodes = self.runtime._popup_ocr_worker.call(compare, timeout=6)
        expires = time.monotonic() + 30
        with self._lock:
            for index, node in enumerate(nodes[:30]):
                box = node['bbox']
                left = x + box[0] * width / 1000
                top = y + box[1] * height / 1000
                right = x + box[2] * width / 1000
                bottom = y + box[3] * height / 1000
                sx, sy = frame.geometry.to_screen(left, top)
                ex, ey = frame.geometry.to_screen(min(right, frame.geometry.image_size[0] - 1),
                                                  min(bottom, frame.geometry.image_size[1] - 1))
                observation.elements.append(Element(f'p{index + 1}', 'popup_ocr', observation.target_id, None,
                    {'raw_text': str(node.get('text', '')), 'confidence': node.get('confidence'),
                     'role': 'text', 'ttl_ms': 30000, 'appearance_verified': False},
                    {'frame_id': frame.frame_id, 'expires_at': expires}, (sx, sy, ex - sx, ey - sy)))
            return [element.public(frame) for element in observation.elements]

    def crop_frame(self, owner, frame_info, region):
        """Retain a crop of the captured image so before/after evidence shares its timestamp."""
        import numpy as np

        with self._lock:
            observation = self.get(owner, frame_info['observation_id'])
            source = next((frame for frame in observation.frames if frame.frame_id == frame_info['frame_id']), None)
            if source is None:
                raise RuntimeFault('FRAME_EXPIRED', 'Source frame is absent from this observation')
            pixels = cv2.imdecode(np.frombuffer(self.image(owner, source.frame_id), np.uint8), cv2.IMREAD_COLOR)
            x, y, width, height = (region[key] for key in ('x', 'y', 'width', 'height'))
            if (pixels is None or any(not isinstance(value, int) for value in (x, y, width, height))
                    or min(x, y) < 0 or min(width, height) <= 0
                    or x + width > pixels.shape[1] or y + height > pixels.shape[0]):
                raise RuntimeFault('INVALID_REGION', 'Evidence crop must fit within the source image')
            ok, encoded = cv2.imencode('.jpg', pixels[y:y + height, x:x + width], [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok or encoded.nbytes > 4 * 1024 * 1024:
                raise RuntimeFault('IMAGE_TOO_LARGE', 'Could not store the evidence crop')
            left, top, source_width, source_height = source.geometry.crop or (0, 0, *source.geometry.source_rect[2:])
            scale_x = source_width / pixels.shape[1]
            scale_y = source_height / pixels.shape[0]
            geometry = replace(source.geometry, image_size=(width, height),
                               crop=(left + x * scale_x, top + y * scale_y,
                                     width * scale_x, height * scale_y))
            target = self.runtime.resolve_target(owner, observation.target_id)
            frame = self.runtime.frames.add(target, geometry, revision=source.revision)
            observation.frames.append(frame)
            observation.images[frame.frame_id] = encoded.tobytes()
            self._prune()
            return {**frame.summary(), 'observation_id': observation.observation_id,
                    'source_frame_id': source.frame_id, 'captured_at': observation.captured_at}

    def previous_click(self, owner, before_info, after_info, point):
        """Use retained snapshots for a bounded, session-scoped reversal check."""
        with self._lock:
            current = self.get(owner, after_info['observation_id'])
            current.metadata['click'] = {'before': before_info, 'after': after_info, 'point': point}
            geometry = current.frames[0].geometry
            for snapshot in reversed(list(self._snapshots.values())):
                click = snapshot.metadata.get('click')
                if (snapshot is current or snapshot.owner != owner or snapshot.target_id != current.target_id
                        or snapshot.created < time.monotonic() - 30 or not click):
                    continue
                if (snapshot.frames[0].geometry != geometry
                        or max(abs(a - b) for a, b in zip(point, click['point'])) > 4):
                    continue
                try:
                    return (self.image(owner, click['before']['frame_id']), self.image(owner, click['after']['frame_id']))
                except RuntimeFault:
                    return None
        return None

    def element(self, owner, observation_id, target_id, ref):
        observation = self.get(owner, observation_id, target_id)
        element = next((element for element in observation.elements if element.ref == ref), None)
        if element is None:
            raise RuntimeFault('REF_NOT_FOUND', 'Reference is absent from this observation')
        if element.locator.get('expires_at', float('inf')) <= time.monotonic():
            raise RuntimeFault('REF_STALE', 'Transient visual reference expired. Observe again')
        target = self.runtime.resolve_target(owner, element.target_id)
        if element.document_id and target.metadata.get('document_id') != element.document_id:
            raise RuntimeFault('REF_STALE', 'Document navigated. Observe the current document')
        return element, target

    def dom_element(self, owner, element):
        from .dom import RESOLVE_JS
        target = self.runtime.browser.resolve(owner, element.target_id)
        if target.metadata['document_id'] != element.document_id:
            raise RuntimeFault('REF_STALE', 'Reference belongs to an earlier document')
        handle = target.handle[1].evaluate_handle(RESOLVE_JS,
            {'key': self.runtime.browser.document_key, 'token': element.locator['token']})
        node = handle.as_element()
        if node is None:
            handle.dispose()
            raise RuntimeFault('REF_STALE', 'Observed DOM element was removed')
        live = node.evaluate('''el => ({connected: el.isConnected,
          name: String(el.getAttribute('aria-label') || (el.labels ? Array.from(el.labels).map(label => label.innerText).join(' ') : '')
             || el.getAttribute('title') || el.getAttribute('placeholder') || el.innerText || el.textContent || '').trim().slice(0, 4000)})''')
        if not live['connected'] or live['name'] != element.semantic['name']:
            node.dispose()
            raise RuntimeFault('REF_STALE', 'Observed DOM element identity or label changed')
        return node

    def _dom(self, owner, target_id, trace):
        from .dom import _SNAPSHOT_JS
        target = self.runtime.browser.resolve(owner, target_id)
        page, selected_frame = target.handle
        dialog = self.runtime.browser.dialog_metadata(page)
        if dialog:
            trace['dom'] += 1
            return [], dialog['message'], {'dialog': dialog}
        frames = page.frames if selected_frame is page.main_frame else [selected_frame]
        elements, bodies, children, warnings, versions = [], [], [], [], []
        trace['dom'] += 1
        trace['dom_frames'] = 0
        for frame in frames[:30]:
            if len(elements) >= 3000:
                warnings.append('DOM_SOURCE_TRUNCATED')
                break
            try:
                source = self.runtime.browser._adopt(owner, page, frame)
                versions.append((source.target_id, source.revision))
                snapshot = frame.evaluate(_SNAPSHOT_JS, {'key': self.runtime.browser.document_key, 'limit': 1500})
                trace['dom_frames'] += 1
                offset = source.metadata.get('frame_bounds') or {'x': 0, 'y': 0}
                for node in snapshot.get('elements', []):
                    semantic = {key: node[key] for key in ('role', 'name', 'value', 'enabled', 'checked', 'selected',
                                                          'editable', 'focused', 'expanded', 'options', 'options_truncated', 'input_type') if key in node}
                    if node.get('in_overlay'):
                        semantic['in_overlay'] = True
                    rect = node['rect']
                    rect = (rect[0] + offset['x'], rect[1] + offset['y'], rect[2], rect[3])
                    elements.append(Element('', 'dom', source.target_id, source.metadata['document_id'], semantic,
                                            {'token': node['token'], 'selector': node['selector']}, rect))
                bodies.append(snapshot.get('text', ''))
                if frame is not selected_frame:
                    children.append({'target_id': source.target_id, 'kind': source.kind, 'revision': source.revision,
                                     'title': source.metadata['title'][:120], 'url': source.metadata['url'][:300],
                                     'tab_id': source.metadata['tab_id'],
                                     'frame_name': source.metadata['frame_name'][:80]})
                if snapshot.get('hit_cap') or snapshot.get('text_truncated'):
                    warnings.append('DOM_SOURCE_TRUNCATED')
            except Exception as error:
                warnings.append(f'DOM_FRAME_UNAVAILABLE: {type(error).__name__}')
        elements.sort(key=lambda element: not element.semantic.get('in_overlay', False))
        return elements, '\n'.join(bodies)[:60000], {'targets': children, 'warnings': warnings,
                                                      'source_truncated': bool(warnings) or len(frames) > 30,
                                                      'source_versions': versions}

    def _native(self, owner, target_id, trace, budget=2):
        from pywinauto import Desktop
        from .desktop_tools import _uia_batch_elements
        self._release_pending_uia()
        target = self.runtime.resolve_target(owner, target_id)
        root = Desktop(backend='uia').window(handle=target.handle).wrapper_object()
        root.element_info.set_cache_strategy(cached=True)
        region = self.runtime.window.get_client_region(target.handle)
        if region is None:
            raise RuntimeFault('TARGET_UNAVAILABLE', 'Window client region is unavailable')
        trace['uia'] += 1
        truncation = {}
        nodes = _uia_batch_elements(root, region, 12, 1500, time.monotonic() + budget,
                                    truncation=truncation, semantics=True)
        cutoff = time.monotonic() - self.ttl
        for token in [key for key, value in self._uia_elements.items() if value[0] < cutoff]:
            self._uia_elements.pop(token)
        roles = {'Edit': 'textbox', 'Document': 'document', 'CheckBox': 'checkbox',
                 'ComboBox': 'combobox', 'RadioButton': 'radio', 'ListItem': 'listitem', 'TabItem': 'tab'}

        # CEF can report the same logical control both inside and outside the client area.
        # Region is an object, so compare against its explicit bounds instead of indexing it.
        def inside_client(rect):
            x, y, width, height = rect
            return (region.left <= x and region.top <= y and
                    x + width <= region.left + region.width and
                    y + height <= region.top + region.height)

        filtered_nodes = []
        seen_keys = {}  # (name, width, height) -> index in filtered_nodes
        for node in nodes:
            rect = node['rect']
            key = (node.get('name', ''), rect[2], rect[3])
            previous_index = seen_keys.get(key)
            if previous_index is None:
                seen_keys[key] = len(filtered_nodes)
                filtered_nodes.append(node)
                continue
            previous = filtered_nodes[previous_index]
            if inside_client(rect) and not inside_client(previous['rect']):
                filtered_nodes[previous_index] = node
        nodes = filtered_nodes

        elements = []
        for node in nodes:
            token = uuid.uuid4().hex
            self._uia_elements[token] = (time.monotonic(), owner, node)
            semantic = {key: node[key] for key in ('name', 'value', 'enabled', 'focused', 'editable', 'checked', 'selected', 'expanded') if key in node}
            semantic['role'] = roles.get(node['control_type'], node['control_type'].lower())
            elements.append(Element('', 'uia', target_id, None, semantic,
                                    {'token': token, 'runtime_id': node.get('runtime_id')}, tuple(node['rect'])))
            node['wrapper'].element_info.set_cache_strategy(cached=False)
        while len(self._uia_elements) > 4000:
            self._uia_elements.popitem(last=False)
        text = '\n'.join(str(node.get('value') or node['name']) for node in nodes)
        return elements, text[:60000], {'source_truncated': any(truncation.values()) or len(text) > 60000}

    def uia_element(self, owner, element):
        self._release_pending_uia()
        record = self._uia_elements.get(element.locator['token'])
        if record is None or record[1] != owner or record[0] < time.monotonic() - self.ttl:
            raise RuntimeFault('REF_STALE', 'UIA reference has expired')
        return record[2]

    def validate_uia(self, owner, element, action='click'):
        from .desktop_tools import _uia_live_check, _StaleElementError
        target = self.runtime.resolve_target(owner, element.target_id)
        region = self.runtime.window.get_client_region(target.handle)
        if region is None:
            raise RuntimeFault('REF_STALE', 'Window client region is unavailable')
        node = self.uia_element(owner, element)
        try:
            _uia_live_check(node, region, action)
        except _StaleElementError as error:
            raise RuntimeFault('REF_STALE', str(error)) from error
        return node

    def _release_pending_uia(self):
        with self._lock:
            owners = set(self._pending_uia_owners)
            self._pending_uia_owners.clear()
        for token in [key for key, value in self._uia_elements.items() if value[1] in owners]:
            self._uia_elements.pop(token)

    def _crop(self, owner, target, region):
        frame = self.runtime.frames.get(owner, region['frame_id'], target)
        x, y, width, height = (region[key] for key in ('x', 'y', 'width', 'height'))
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                   for value in (x, y, width, height)):
            raise ValueError('Region coordinates must be finite numbers')
        if min(width, height) <= 0 or x < 0 or y < 0 or x + width > frame.geometry.image_size[0] or y + height > frame.geometry.image_size[1]:
            raise ValueError('Region must fit within its emitted image')
        left, top = frame.geometry.to_source(x, y)
        scale_x, scale_y = frame.geometry.metadata()['scale']
        right, bottom = left + width / scale_x, top + height / scale_y
        return (math.floor(left), math.floor(top), math.ceil(right) - math.floor(left), math.ceil(bottom) - math.floor(top))

    @staticmethod
    def validate_arguments(arguments):
        if not isinstance(arguments, dict) or set(arguments) - {'target_id', 'query', 'region', 'cursor', 'image', 'source', 'include_untitled'}:
            raise ValueError('Unknown observe arguments')
        if 'include_untitled' in arguments and not isinstance(arguments['include_untitled'], bool):
            raise ValueError('include_untitled must be boolean')
        if 'include_untitled' in arguments and (arguments.get('target_id') or arguments.get('cursor')):
            raise ValueError('include_untitled applies to the target directory only')
        if arguments.get('source', 'auto') not in ('auto', 'ocr', 'image', 'combined'):
            raise ValueError('source must be auto, ocr, image or combined')
        if arguments.get('source', 'auto') != 'auto' and not arguments.get('target_id'):
            raise ValueError('target_id is required for an explicit source')
        image, query = arguments.get('image'), arguments.get('query')
        if image is not None and not isinstance(image, bool):
            raise ValueError('Image must be boolean')
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 500):
            raise ValueError('Query must contain 1 to 500 characters')
        for key in ('target_id', 'cursor'):
            if key in arguments and (not isinstance(arguments[key], str) or not arguments[key]):
                raise ValueError(key + ' must be a nonempty string')
        region = arguments.get('region')
        if region is not None:
            if not isinstance(region, dict) or set(region) != {'frame_id', 'x', 'y', 'width', 'height'}:
                raise ValueError('Region requires frame_id, x, y, width and height')
            if not isinstance(region['frame_id'], str) or not region['frame_id']:
                raise ValueError('Region frame_id must be a nonempty string')
            if not all(isinstance(region[key], (int, float)) and not isinstance(region[key], bool) and math.isfinite(region[key])
                       for key in ('x', 'y', 'width', 'height')):
                raise ValueError('Region coordinates must be finite numbers')
            if min(region['x'], region['y']) < 0 or min(region['width'], region['height']) <= 0:
                raise ValueError('Region requires nonnegative origin and positive size')
        if arguments.get('cursor') and any(arguments.get(key) is not None for key in ('query', 'region', 'image', 'source')):
            raise ValueError('Cursor cannot be combined with query, region, image or source')

    def observe(self, owner, target_id=None, *, query=None, region=None, cursor=None, image=None, source='auto', include_untitled=None):
        self.validate_arguments({key: value for key, value in dict(target_id=target_id, query=query, region=region,
                                                                  cursor=cursor, image=image, source=source,
                                                                  include_untitled=include_untitled).items()
                                 if value is not None and (key != 'source' or value != 'auto')})
        if cursor:
            if query is not None or region is not None or image is not None:
                raise ValueError('Cursor cannot be combined with query, region or image')
            try:
                observation_id, element_offset, text_offset = cursor.rsplit('.', 2)
                element_offset, text_offset = int(element_offset), int(text_offset)
                if min(element_offset, text_offset) < 0:
                    raise ValueError()
            except (ValueError, AttributeError) as error:
                raise ValueError('Invalid observation cursor') from error
            observation = self.get(owner, observation_id, target_id)
            if element_offset > len(observation.elements) or text_offset > len(observation.text):
                raise ValueError('Cursor is outside the saved observation')
            return self._page(observation, element_offset, text_offset, include_image=False)
        if target_id is None:
            targets = [{key: row[key] for key in ('target_id', 'kind', 'title', 'url', 'active', 'revision', 'tab_id', 'frame_name',
                                                 'hwnd', 'pid', 'process', 'window_text', 'title_source', 'class_name', 'owner_hwnd') if key in row}
                       for row in self.runtime.target_directory(owner, include_untitled=include_untitled is not False)]
            desktop_id = next(row['target_id'] for row in targets if row['kind'] == 'desktop')
            result = {'success': True, 'targets': targets, 'desktop_target_id': desktop_id}
            if image:
                result.update(self.observe(owner, desktop_id, image=True))
            return result
        target = self.runtime.resolve_target(owner, target_id)
        crop = self._crop(owner, target, region) if region else None
        trace = dict(dom=0, dom_frames=0, uia=0, capture=0, ocr=0, yolo=0, caption=0,
                     dom_ms=0, uia_ms=0, capture_ms=0, ocr_ms=0, yolo_ms=0)
        warnings = []
        started = time.perf_counter()
        semantic_at = timestamp()
        elements, text, metadata = [], '', {}
        revision = target.revision
        uia_coverage = 'not_requested' if target.kind == 'window' and source in ('ocr', 'image') else None
        try:
            backend_started = time.perf_counter()
            if target.kind == 'browser' and source not in ('ocr', 'image'):
                elements, text, metadata = self.runtime.browser.call(self._dom, owner, target_id, trace)
                trace['dom_ms'] = round((time.perf_counter() - backend_started) * 1000, 3)
            if target.kind == 'window' and source not in ('ocr', 'image'):
                try:
                    elements, text, metadata = self.uia.call(self._native, owner, target_id, trace)
                except Exception as error:
                    if 'UIA_BATCH_TIMEOUT' not in str(error):
                        raise
                    elements, text, metadata = self.uia.call(self._native, owner, target_id, trace, 4, timeout=5)
                # A successful scan cannot establish that the provider exposed
                # every visible control, even when many nodes were returned.
                uia_coverage = 'partial' if metadata.get('source_truncated') or len(elements) < 3 else 'unknown'
                trace['uia_ms'] = round((time.perf_counter() - backend_started) * 1000, 3)
        except Exception as error:
            warnings.append(f'SEMANTIC_UNAVAILABLE: {error}')
            if target.kind == 'window':
                uia_coverage = 'timeout' if 'TIMEOUT' in str(error).upper() else 'partial'
        target = self.runtime.resolve_target(owner, target_id)
        if target.revision != revision:
            # Target change during semantic collection (e.g., page navigation) is normal;
            # proceed with the collected semantic but warn that image may be stale.
            warnings.append(f'TARGET_REVISION_CHANGED: Target revision changed from {revision} to {target.revision} during semantic collection (normal during navigation)')
        needle = str(query or '').casefold().strip()
        matched = [element for element in elements if not needle or needle in json.dumps(element.semantic, ensure_ascii=False).casefold()]
        coverage = sum(bool(element.semantic.get('name') or element.semantic.get('value')) for element in matched)
        layout = hashlib.sha256(json.dumps([(element.rect, element.semantic.get('role'), element.semantic.get('name'))
                                            for element in elements], ensure_ascii=False).encode()).hexdigest()
        view = (target.revision, layout)
        semantic_failure = target.kind == 'window' and uia_coverage in ('timeout', 'partial') and not elements
        force_ocr = source in ('ocr', 'combined')
        needs_image = source != 'auto' or semantic_failure or image is True or (image is None and (coverage < 3 or region or self._last_views.get((owner, target_id)) != view))
        if metadata.get('dialog'):
            needs_image = False
            warnings.append('BROWSER_DIALOG_OPEN: use Enter to accept or Escape to dismiss. Semantic dialog evidence is available')
        pixels = frame = None
        if not metadata.get('dialog') and (needs_image or (needle and not coverage) or region):
            capture_started = time.perf_counter()
            pixels, frame = self.runtime.capture_target(owner, target_id, max_dim=None if region else 1366, crop=crop)
            trace['capture'] += 1
            trace['capture_ms'] = round((time.perf_counter() - capture_started) * 1000, 3)
        if source != 'image' and not metadata.get('dialog') and (force_ocr or (needle and not coverage) or region or semantic_failure):
            for backend in ('ocr', 'yolo'):
                if backend == 'yolo' and force_ocr:
                    break
                if backend == 'yolo' and semantic_failure and not (needle or region):
                    break
                if backend == 'yolo' and coverage:
                    break
                try:
                    trace[backend] += 1
                    backend_started = time.perf_counter()
                    if backend == 'ocr':
                        # Explicit visual searches may include cold model initialization.
                        # Automatic fallback stays short to keep normal observation responsive.
                        timeout = 10 if force_ocr or needle or region else 3
                        nodes = self.runtime._popup_ocr_worker.call(self.runtime.perceive, pixels, backend, timeout=timeout)
                    else:
                        nodes = self.runtime.perceive(pixels, backend)
                    for node in nodes:
                        name = str(node.get('text') or node.get('label') or '')
                        if needle and needle not in name.casefold():
                            continue
                        box = node['bbox']
                        x, y = frame.geometry.to_screen(box[0] * pixels.shape[1] / 1000, box[1] * pixels.shape[0] / 1000)
                        width = (box[2] - box[0]) / 1000 * (frame.geometry.crop or frame.geometry.source_rect)[2]
                        height = (box[3] - box[1]) / 1000 * (frame.geometry.crop or frame.geometry.source_rect)[3]
                        semantic = ({'raw_text': name, 'role': 'text', 'confidence': node.get('confidence')}
                                    if backend == 'ocr' else {'name': name, 'role': 'button', 'confidence': node.get('confidence')})
                        if backend == 'ocr':
                            semantic['hit_test_confidence'] = None
                            if source == 'combined':
                                semantic['semantic_missing'] = not any(
                                    element.rect and element.rect[0] < x + width and element.rect[0] + element.rect[2] > x
                                    and element.rect[1] < y + height and element.rect[1] + element.rect[3] > y
                                    and name.casefold() in str(element.semantic.get('name') or element.semantic.get('value') or '').casefold()
                                    for element in elements)
                                if semantic['semantic_missing'] and target.kind == 'window' and uia_coverage != 'timeout':
                                    uia_coverage = 'partial'
                        matched.append(Element('', backend, target_id, target.metadata.get('document_id'),
                                               semantic,
                                               {'frame_id': frame.frame_id}, (x, y, width, height)))
                        coverage += bool(name)
                except Exception as error:
                    warnings.append(f'{backend.upper()}_UNAVAILABLE: {error}. Run scripts/bootstrap.ps1 -Vision to provision local vision; supply -WeightsDir for existing icon weights')
                finally:
                    trace[backend + '_ms'] = round((time.perf_counter() - backend_started) * 1000, 3)
        if crop:
            origin = frame.geometry.source_rect[:2]
            left, top, width, height = crop
            left, top = left + origin[0], top + origin[1]
            matched = [element for element in matched if element.rect and
                       element.rect[0] < left + width and element.rect[0] + element.rect[2] > left and
                       element.rect[1] < top + height and element.rect[1] + element.rect[3] > top]
        for index, element in enumerate(matched):
            element.ref = f'{"v" if element.source in ("ocr", "yolo") else "e"}{index + 1}'
        if needle:
            text = '\n'.join(line for line in text.splitlines() if needle in line.casefold())
            text = '\n'.join(filter(None, [text, *(element.semantic.get('raw_text', '') for element in matched if element.source == 'ocr')]))
        elif semantic_failure or force_ocr:
            text = '\n'.join(filter(None, [text, *(element.semantic.get('raw_text', '') for element in matched if element.source == 'ocr')]))
        versions = metadata.pop('source_versions', [(target_id, revision)])
        for source_id, source_revision in versions:
            if self.runtime.resolve_target(owner, source_id).revision != source_revision:
                if frame:
                    self.runtime.frames.discard(frame.frame_id)
                raise RuntimeFault('TARGET_CHANGED', 'A source frame changed between semantic and image collection')
        images = {}
        if frame and needs_image:
            ok, encoded = cv2.imencode('.jpg', pixels, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok or encoded.nbytes > 4 * 1024 * 1024:
                self.runtime.frames.discard(frame.frame_id)
                raise RuntimeFault('IMAGE_TOO_LARGE', 'Request a smaller image region')
            images[frame.frame_id] = encoded.tobytes()
        self._last_views[(owner, target_id)] = view
        observation = Observation('o_' + uuid.uuid4().hex, owner, target_id, target.revision,
                                  semantic_at, matched, text,
                                  {**metadata, 'document_id': target.metadata.get('document_id'),
                                   'title': target.metadata.get('title', '')[:512], 'url': (target.metadata.get('url') or '')[:2048],
                                   'warnings': metadata.get('warnings', []) + warnings, 'query': query, 'source': source,
                                   **({'uia_coverage': uia_coverage} if uia_coverage else {}),
                                   **({'integrity': integrity_evidence(target.metadata.get('pid'))} if target.kind == 'window' else {}),
                                   'trace': {**trace, 'elapsed_ms': round((time.perf_counter() - started) * 1000, 3)},
                                   'evidence': {'semantic_at': semantic_at, 'image_at': timestamp() if images else None}},
                                  [frame] if frame else [], images)
        with self._lock:
            self._snapshots[observation.observation_id] = observation
            self._prune()
        return self._page(observation)

    def _page(self, observation, element_offset=0, text_offset=0, *, include_image=True):
        frame = observation.frames[0] if observation.images and include_image else None
        elements = []
        for element in observation.elements[element_offset:element_offset + 80]:
            public = element.public(frame)
            if elements and len(json.dumps(elements + [public], ensure_ascii=False).encode()) > 45000:
                break
            elements.append(public)
        text = observation.text[text_offset:text_offset + 6000]
        next_element, next_text = element_offset + len(elements), text_offset + len(text)
        result = {'observation_id': observation.observation_id, 'target_id': observation.target_id,
                  'revision': observation.revision, 'captured_at': observation.captured_at,
                  **observation.metadata, 'elements': elements, 'text': text,
                  'truncated': {'elements': next_element < len(observation.elements), 'text': next_text < len(observation.text)},
                  'frames': [frame.summary()] if frame else []}
        if any(result['truncated'].values()):
            result['next_cursor'] = f'{observation.observation_id}.{next_element}.{next_text}'
        return {'success': True, 'observation': result}

    def release_target(self, owner, target_id):
        with self._lock:
            for key in [key for key, value in self._snapshots.items()
                        if value.owner == owner and value.target_id == target_id]:
                self._drop(key)
            self._last_views.pop((owner, target_id), None)

    def release(self, owner):
        with self._lock:
            for key in [key for key, value in self._snapshots.items() if value.owner == owner]:
                self._drop(key)
            self._last_views = {key: value for key, value in self._last_views.items() if key[0] != owner}
            self._pending_uia_owners.add(owner)
        pending = False
        if self.uia._thread is not None:
            try:
                self.uia.call(self._release_pending_uia, timeout=1)
            except RuntimeFault:
                pending = True
                logger.warning('UIA cleanup remains pending until its provider returns')
        return {'uia_cleanup_pending': pending}

    def shutdown(self):
        self.uia.shutdown()
        with self._lock:
            for key in list(self._snapshots):
                self._drop(key)
