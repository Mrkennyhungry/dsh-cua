"""Bounded semantic snapshots, stable references and owner-scoped image resources."""
from collections import OrderedDict
from dataclasses import dataclass, field
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
        value = {'ref': self.ref, **self.semantic}
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
                raise RuntimeFault('TARGET_MISMATCH', 'Observation belongs to another target')
            return value

    def image(self, owner, frame_id):
        with self._lock:
            self._prune()
            for observation in self._snapshots.values():
                if observation.owner == owner and frame_id in observation.images:
                    return observation.images[frame_id]
        raise RuntimeFault('FRAME_EXPIRED', 'Image resource has expired')

    def element(self, owner, observation_id, target_id, ref):
        observation = self.get(owner, observation_id, target_id)
        element = next((element for element in observation.elements if element.ref == ref), None)
        if element is None:
            raise RuntimeFault('REF_NOT_FOUND', 'Reference is absent from this observation')
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
        from .web_tools import _SNAPSHOT_JS
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
                                                          'editable', 'focused', 'options', 'options_truncated', 'input_type') if key in node}
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
                                     'frame_name': source.metadata['frame_name'][:80]})
                if snapshot.get('hit_cap') or snapshot.get('text_truncated'):
                    warnings.append('DOM_SOURCE_TRUNCATED')
            except Exception as error:
                warnings.append(f'DOM_FRAME_UNAVAILABLE: {type(error).__name__}')
        elements.sort(key=lambda element: not element.semantic.get('in_overlay', False))
        return elements, '\n'.join(bodies)[:60000], {'targets': children, 'warnings': warnings,
                                                      'source_truncated': bool(warnings) or len(frames) > 30,
                                                      'source_versions': versions}

    def _native(self, owner, target_id, trace):
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
        nodes = _uia_batch_elements(root, region, 12, 1500, time.monotonic() + 2,
                                    truncation=truncation, semantics=True)
        cutoff = time.monotonic() - self.ttl
        for token in [key for key, value in self._uia_elements.items() if value[0] < cutoff]:
            self._uia_elements.pop(token)
        roles = {'Edit': 'textbox', 'Document': 'document', 'CheckBox': 'checkbox',
                 'ComboBox': 'combobox', 'RadioButton': 'radio', 'ListItem': 'listitem', 'TabItem': 'tab'}
        elements = []
        for node in nodes:
            token = uuid.uuid4().hex
            self._uia_elements[token] = (time.monotonic(), owner, node)
            semantic = {key: node[key] for key in ('name', 'value', 'enabled', 'focused', 'editable', 'checked', 'selected') if key in node}
            semantic['role'] = roles.get(node['control_type'], node['control_type'].lower())
            elements.append(Element('', 'uia', target_id, None, semantic, {'token': token}, tuple(node['rect'])))
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
        if not isinstance(arguments, dict) or set(arguments) - {'target_id', 'query', 'region', 'cursor', 'image'}:
            raise ValueError('Unknown observe arguments')
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
        if arguments.get('cursor') and any(arguments.get(key) is not None for key in ('query', 'region', 'image')):
            raise ValueError('Cursor cannot be combined with query, region or image')

    def observe(self, owner, target_id=None, *, query=None, region=None, cursor=None, image=None):
        self.validate_arguments({key: value for key, value in dict(target_id=target_id, query=query, region=region,
                                                                  cursor=cursor, image=image).items() if value is not None})
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
            targets = [{key: row[key] for key in ('target_id', 'kind', 'title', 'url', 'active', 'revision', 'frame_name') if key in row}
                       for row in self.runtime.target_directory(owner)]
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
        try:
            backend_started = time.perf_counter()
            if target.kind == 'browser':
                elements, text, metadata = self.runtime.browser.call(self._dom, owner, target_id, trace)
                trace['dom_ms'] = round((time.perf_counter() - backend_started) * 1000, 3)
            if target.kind == 'window':
                elements, text, metadata = self.uia.call(self._native, owner, target_id, trace)
                trace['uia_ms'] = round((time.perf_counter() - backend_started) * 1000, 3)
        except Exception as error:
            warnings.append(f'SEMANTIC_UNAVAILABLE: {error}')
        target = self.runtime.resolve_target(owner, target_id)
        if target.revision != revision:
            raise RuntimeFault('TARGET_CHANGED', 'Target changed during semantic collection. Observe again')
        needle = str(query or '').casefold().strip()
        matched = [element for element in elements if not needle or needle in json.dumps(element.semantic, ensure_ascii=False).casefold()]
        coverage = sum(bool(element.semantic.get('name') or element.semantic.get('value')) for element in matched)
        layout = hashlib.sha256(json.dumps([(element.rect, element.semantic.get('role'), element.semantic.get('name'))
                                            for element in elements], ensure_ascii=False).encode()).hexdigest()
        view = (target.revision, layout)
        needs_image = image is True or (image is None and (coverage < 3 or region or self._last_views.get((owner, target_id)) != view))
        if metadata.get('dialog'):
            needs_image = False
            warnings.append('BROWSER_DIALOG_OPEN: use Enter to accept or Escape to dismiss. Semantic dialog evidence is available')
        pixels = frame = None
        if not metadata.get('dialog') and (needs_image or (needle and not coverage) or region):
            capture_started = time.perf_counter()
            pixels, frame = self.runtime.capture_target(owner, target_id, max_dim=None if region else 1366, crop=crop)
            trace['capture'] += 1
            trace['capture_ms'] = round((time.perf_counter() - capture_started) * 1000, 3)
        if not metadata.get('dialog') and ((needle and not coverage) or region):
            for backend in ('ocr', 'yolo'):
                if backend == 'yolo' and coverage:
                    break
                try:
                    trace[backend] += 1
                    backend_started = time.perf_counter()
                    nodes = self.runtime.perceive(pixels, backend)
                    for node in nodes:
                        name = str(node.get('text') or node.get('label') or '')
                        if needle and needle not in name.casefold():
                            continue
                        box = node['bbox']
                        x, y = frame.geometry.to_screen(box[0] * pixels.shape[1] / 1000, box[1] * pixels.shape[0] / 1000)
                        width = (box[2] - box[0]) / 1000 * (frame.geometry.crop or frame.geometry.source_rect)[2]
                        height = (box[3] - box[1]) / 1000 * (frame.geometry.crop or frame.geometry.source_rect)[3]
                        matched.append(Element('', backend, target_id, target.metadata.get('document_id'),
                                               {'name': name, 'role': 'text' if backend == 'ocr' else 'button'},
                                               {'frame_id': frame.frame_id}, (x, y, width, height)))
                        coverage += bool(name)
                except Exception as error:
                    warnings.append(f'{backend.upper()}_UNAVAILABLE: {error}. Run scripts/bootstrap.ps1 to provision local vision')
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
            element.ref = f'e{index + 1}'
        if needle:
            text = '\n'.join(line for line in text.splitlines() if needle in line.casefold())
            text = '\n'.join(filter(None, [text, *(element.semantic.get('name', '') for element in matched if element.source == 'ocr')]))
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
                                   'warnings': metadata.get('warnings', []) + warnings, 'query': query,
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
