"""Thread-affine browser targets backed by the existing Playwright worker."""
import hashlib
import json
import os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

from .targets import RuntimeFault
from .dom import STATE_JS

_WEB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dsh-web')


class BrowserRuntime:
    def __init__(self, runtime):
        self.runtime = runtime
        self._requests = OrderedDict()
        self._page_ids = {}
        self._documents = {}
        self._connections = {}
        self.profile = None
        self.thread_id = None
        self._context_instance = None
        self._owned_pages = {}
        self._playwright = None
        self._pages_by_id = {}
        self._profile_lease = None
        self.document_key = '__dsh_' + runtime.epoch
        self.pending_dialogs = {}

    def _dialog(self, page, dialog):
        self.pending_dialogs[page] = dialog
        for target in self.runtime.targets.records_for_handle(page):
            self.runtime.targets.refresh(target, {**target.metadata, 'dialog': self.dialog_metadata(page)})

    def dialog_metadata(self, page):
        dialog = self.pending_dialogs.get(page)
        if dialog is None:
            return None
        return {'type': dialog.type, 'message': dialog.message[:4000], 'default_value': dialog.default_value[:1000]}

    def call(self, function, *args, timeout=30, **kwargs):
        if self.thread_id == threading.get_ident():
            return function(*args, **kwargs)
        def invoke():
            self.thread_id = threading.get_ident()
            return function(*args, **kwargs)
        future = _WEB_EXECUTOR.submit(invoke)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    def _context(self):
        from . import browser_support as web
        self.thread_id = threading.get_ident()
        if self._context_instance is not None:
            try:
                browser = self._context_instance.browser
                if browser is None or browser.is_connected():
                    self._context_instance.pages
                    return self._context_instance
            except Exception:
                pass
            try:
                self._context_instance.close()
            except Exception:
                pass
            self._context_instance = None
        home = self.runtime.home
        name = 'web_profile_edge' if web._browser_mode() == 'edge' else 'web_profile'
        legacy = home / name
        self.profile = legacy if legacy.exists() else self.runtime.state_dir / 'browser' / name
        self.profile.mkdir(parents=True, exist_ok=True)
        self._acquire_profile()
        os.environ['DSH_DESKTOP_BROWSER_PROFILE'] = str(self.profile)
        try:
            web._prepare_playwright_temp_dir()
            self._context_instance = web._launch_persistent_context(self._driver())
        except Exception as error:
            self._release_profile()
            raise RuntimeFault('BROWSER_PROFILE_UNAVAILABLE', f'Could not open persistent profile {self.profile}: {error}') from error
        return self._context_instance

    def _acquire_profile(self):
        import psutil
        import win32api
        import win32event
        self._release_profile()
        profile = os.path.normcase(str(self.profile.resolve()))
        digest = hashlib.sha256(profile.encode()).hexdigest()
        lease = win32event.CreateMutex(None, False, f'Global\\DSHDoraemonBrowser_{digest}')
        if win32api.GetLastError() == 183:
            win32api.CloseHandle(lease)
            raise RuntimeFault('BROWSER_PROFILE_UNAVAILABLE', f'Persistent profile is already owned by another runtime: {self.profile}')
        self._profile_lease = lease
        for process in psutil.process_iter(['cmdline']):
            args = process.info.get('cmdline') or []
            paths = [arg.split('=', 1)[1] for arg in args if arg.startswith('--user-data-dir=')]
            if '--user-data-dir' in args:
                index = args.index('--user-data-dir')
                paths.extend(args[index + 1:index + 2])
            if any(os.path.normcase(os.path.abspath(path.strip('"'))) == profile for path in paths):
                self._release_profile()
                raise RuntimeFault('BROWSER_PROFILE_UNAVAILABLE', f'Persistent profile is in use by PID {process.pid}: {self.profile}')

    def _release_profile(self):
        if self._profile_lease is not None:
            import win32api
            win32api.CloseHandle(self._profile_lease)
            self._profile_lease = None

    def _driver(self):
        from playwright.sync_api import sync_playwright
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        return self._playwright

    def _adopt(self, owner, page, frame=None):
        if page not in self._page_ids:
            session = page.context.new_cdp_session(page)
            try:
                tab_id = session.send('Target.getTargetInfo')['targetInfo']['targetId']
            finally:
                session.detach()
            canonical = self._pages_by_id.get(tab_id)
            if canonical is not None and not canonical.is_closed():
                page = canonical
                frame = None
            else:
                self._pages_by_id[tab_id] = page
                self._page_ids[page] = tab_id
                self._documents[page] = {}
                page.on('framenavigated', lambda changed: self._documents[page].__setitem__(changed, uuid.uuid4().hex))
                page.on('close', self._forget_page)
                page.on('dialog', lambda dialog: self._dialog(page, dialog))
        frame = frame or page.main_frame
        documents = self._documents[page]
        documents.setdefault(frame, uuid.uuid4().hex)
        identity = (self._page_ids[page], frame)
        document_revision = frame.evaluate(STATE_JS, self.document_key)
        layout = frame.evaluate('''() => ({x: scrollX, y: scrollY, width: innerWidth,
          height: innerHeight, dpi: devicePixelRatio, length: document.documentElement?.scrollHeight})''')
        parent = frame.parent_frame
        bounds = frame.frame_element().bounding_box() if parent else None
        metadata = {'title': page.title(), 'url': frame.url,
                    'tab_id': self._page_ids[page], 'document_id': documents[frame],
                    'frame_name': frame.name, 'visible': not page.is_closed(),
                    'layout': layout, 'frame_bounds': bounds, 'document_revision': document_revision}
        if frame is page.main_frame:
            descendants = []
            for child in page.frames[:30]:
                if child is frame:
                    continue
                try:
                    descendants.append((documents.setdefault(child, uuid.uuid4().hex), child.url,
                                        child.evaluate(STATE_JS, self.document_key), child.frame_element().bounding_box()))
                except Exception:
                    descendants.append(('unavailable', child.url))
            metadata['descendants'] = descendants
        return self.runtime.targets.adopt(owner, 'browser', identity, (page, frame), metadata)

    def discover(self, owner):
        def collect():
            contexts = {page.context for page in self._page_ids if not page.is_closed()}
            for context in contexts:
                for page in context.pages:
                    if page.is_closed() or page in self._page_ids:
                        continue
                    # Popups inherit the opener's ownership, including nested
                    # popups. Unrelated persistent tabs need explicit connection.
                    opener = page.opener()
                    while opener is not None and opener not in self._page_ids:
                        opener = opener.opener()
                    owners = {target.owner for target in self.runtime.targets.records_for_handle(opener)} if opener else set()
                    if owner in owners:
                        self._owned_pages[page] = self._owned_pages.get(opener, owner)
                        self._adopt(owner, page)
        return self.call(collect)

    def _forget_page(self, page):
        tab_id = self._page_ids.pop(page, None)
        self._pages_by_id.pop(tab_id, None)
        self._documents.pop(page, None)
        self._owned_pages.pop(page, None)
        self.pending_dialogs.pop(page, None)
        self.runtime.targets.invalidate(lambda target: target.kind == 'browser' and target.handle[0] is page)

    def open(self, owner, url, call_id, target_id=None):
        return self.call(self._open, owner, url, call_id, target_id)

    def _open(self, owner, url, call_id, target_id):
        fingerprint = hashlib.sha256(json.dumps([url, target_id]).encode()).hexdigest()
        key = (owner, call_id)
        if call_id and key in self._requests:
            digest, existing, failure = self._requests[key]
            if digest != fingerprint:
                raise RuntimeFault('REQUEST_CONFLICT', 'Call identity was already used for a different open request')
            if failure:
                raise RuntimeFault('NAVIGATION_FAILED', failure)
            return self.resolve(owner, existing)
        if call_id and len(self._requests) >= 1024:
            raise RuntimeFault('REQUEST_LIMIT', 'Release finished session resources before opening more targets')
        if target_id:
            target = self.resolve(owner, target_id)
            page, frame = target.handle
        else:
            context = self._context()
            page = context.new_page()
            frame = page.main_frame
            self._owned_pages[page] = owner
            # Record identity before navigation so a navigation error cannot
            # cause a retry to create another tab.
            target = self._adopt(owner, page, frame)
        if call_id:
            self._requests[key] = fingerprint, target.target_id, None
        try:
            if url:
                frame.goto(url, wait_until='domcontentloaded', timeout=15000)
        except Exception as error:
            message = f'Navigation failed for {target.target_id}. Inspect this target before retrying with a new call identity: {error}'
            if call_id:
                self._requests[key] = fingerprint, target.target_id, message
            raise RuntimeFault('NAVIGATION_FAILED', message) from error
        page.bring_to_front()
        return self._adopt(owner, page, frame)

    def resolve(self, owner, target_id):
        # Called only on the shared browser worker.
        target = self.runtime.targets.get(owner, target_id)
        if target.kind != 'browser':
            raise RuntimeFault('TARGET_KIND', 'The selected target is not a browser frame')
        page, frame = target.handle
        if page in self.pending_dialogs:
            return target
        if page.is_closed() or frame.is_detached():
            target.closed = True
            raise RuntimeFault('TARGET_CLOSED', 'Browser page or frame has closed')
        try:
            return self._adopt(owner, page, frame)
        except Exception as error:
            if page.is_closed() or frame.is_detached():
                target.closed = True
                raise RuntimeFault('TARGET_CLOSED', 'Browser page or frame closed during validation') from error
            raise

    def frames(self, owner, target_id):
        def collect():
            page, _ = self.resolve(owner, target_id).handle
            return [self._adopt(owner, page, frame).summary() for frame in page.frames]
        return self.call(collect)

    def connect(self, owner, endpoint):
        def attach():
            self.thread_id = threading.get_ident()
            pw = self._driver()
            browser = self._connections.get(endpoint)
            if browser is None or not browser.is_connected():
                if len(self._connections) >= 32:
                    raise RuntimeFault('REQUEST_LIMIT', 'Too many explicit browser connections')
                try:
                    browser = pw.chromium.connect_over_cdp(endpoint, timeout=10000)
                except Exception as error:
                    raise RuntimeFault('BACKEND_UNAVAILABLE', str(error)) from error
                self._connections[endpoint] = browser
            return [self._adopt(owner, page).summary() for context in browser.contexts for page in context.pages]
        return self.call(attach)

    def close_target(self, owner, target_id, *, respect_before_unload=False):
        def close():
            target = self.resolve(owner, target_id)
            page, frame = target.handle
            if frame is not page.main_frame:
                raise RuntimeFault('TARGET_KIND', 'Close the containing tab to close this frame')
            page.close(run_before_unload=respect_before_unload)
            closed = page.is_closed()
            if closed:
                self.runtime.targets.invalidate(lambda record: record.kind == 'browser' and record.handle[0] is page)
            return {'success': True, 'target_id': target_id, 'closed': closed}
        return self.call(close)

    def capture(self, owner, target_id):
        def capture():
            import cv2
            import numpy as np
            from .geometry import FrameGeometry
            target = self.resolve(owner, target_id)
            page, frame = target.handle
            bounds = target.metadata['frame_bounds']
            clip = bounds if bounds else None
            # Playwright's default caret hiding mutates input style attributes.
            # Keep the observed page intact so our mutation revision stays real.
            raw = page.screenshot(type='png', scale='css', clip=clip, caret='initial', timeout=10000)
            image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
            height, width = image.shape[:2]
            # Browser points remain page viewport CSS pixels. They are dispatched
            # by Playwright on the owning worker, independent of desktop DPI.
            source = ((bounds['x'], bounds['y'], bounds['width'], bounds['height'])
                      if bounds else (0, 0, width, height))
            dpi = round(96 * target.metadata['layout']['dpi'])
            geometry = FrameGeometry(source, (width, height), method='playwright',
                                     coordinate_space='browser_css', dpi=(dpi, dpi))
            after = self.resolve(owner, target_id)
            return image, geometry, after
        return self.call(capture)

    def release(self, owner):
        def release():
            for page, page_owner in list(self._owned_pages.items()):
                if page_owner == owner:
                    shared = [target.owner for target in self.runtime.targets.records_for_handle(page)
                              if target.owner != owner]
                    if shared:
                        self._owned_pages[page] = shared[0]
                        continue
                    if not page.is_closed():
                        page.close()
                    self._forget_page(page)
            self._requests = OrderedDict((key, value) for key, value in self._requests.items() if key[0] != owner)
        return self.call(release)

    def shutdown(self):
        def close():
            if self._context_instance is not None:
                try:
                    self._context_instance.close()
                except Exception:
                    pass
            if self._playwright is not None:
                self._playwright.stop()
            self._playwright = None
            self._connections.clear()
            self._context_instance = None
            self._owned_pages.clear()
            self._page_ids.clear()
            self._documents.clear()
            self._pages_by_id.clear()
            self.pending_dialogs.clear()
            self._release_profile()
        return self.call(close, timeout=5)
