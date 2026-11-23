"""Owner-scoped targets with live object and geometry validation."""
from dataclasses import dataclass, field
from collections import OrderedDict
import threading
import time
import uuid


class RuntimeFault(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass
class Target:
    target_id: str
    owner: str
    kind: str
    identity: object
    handle: object = field(repr=False)
    metadata: dict = field(default_factory=dict)
    revision: int = 1
    closed: bool = False

    def summary(self):
        result = {'target_id': self.target_id, 'kind': self.kind, 'revision': self.revision,
                  'closed': self.closed, **self.metadata}
        for key, limit in (('title', 512), ('url', 2048)):
            if isinstance(result.get(key), str) and len(result[key]) > limit:
                result[key] = result[key][:limit]
                result[key + '_truncated'] = True
        return result


class TargetRegistry:
    def __init__(self, epoch):
        self.epoch = epoch
        self._targets = OrderedDict()
        self._lock = threading.RLock()

    def adopt(self, owner, kind, identity, handle, metadata):
        with self._lock:
            target = next((target for target in self._targets.values()
                           if target.owner == owner and target.kind == kind and target.identity == identity and not target.closed), None)
            if target is None:
                target_id = f't_{self.epoch}_{uuid.uuid4().hex}'
                target = Target(target_id, owner, kind, identity, handle)
                self._targets[target_id] = target
            self.refresh(target, metadata)
            return target

    def get(self, owner, target_id):
        with self._lock:
            if not str(target_id).startswith(f't_{self.epoch}_'):
                raise RuntimeFault('RUNTIME_RESTARTED', 'Refresh the target directory after runtime restart')
            target = self._targets.get(target_id)
            if target is None or target.owner != owner:
                raise RuntimeFault('TARGET_NOT_FOUND', 'Target is unavailable in this session')
            if target.closed:
                raise RuntimeFault('TARGET_CLOSED', 'Target has closed')
            return target

    def refresh(self, target, metadata):
        with self._lock:
            if target.metadata and target.metadata != metadata:
                target.revision += 1
            target.metadata = dict(metadata)

    def release(self, owner):
        with self._lock:
            self._targets = {key: target for key, target in self._targets.items() if target.owner != owner}

    def invalidate(self, predicate):
        with self._lock:
            for target in self._targets.values():
                if predicate(target):
                    target.closed = True

    def records(self, owner):
        with self._lock:
            return [target for target in self._targets.values() if target.owner == owner and not target.closed]

    def records_for_handle(self, page):
        with self._lock:
            return [target for target in self._targets.values()
                    if target.kind == 'browser' and not target.closed and target.handle[0] is page]

    def directory(self, owner):
        with self._lock:
            return [target.summary() for target in self._targets.values() if target.owner == owner and not target.closed]


@dataclass(frozen=True)
class Frame:
    frame_id: str
    owner: str
    target_id: str
    revision: int
    geometry: object
    created: float = field(default_factory=time.monotonic)

    def summary(self):
        return {'frame_id': self.frame_id, 'target_id': self.target_id,
                'revision': self.revision, 'width': self.geometry.image_size[0],
                'height': self.geometry.image_size[1], **self.geometry.metadata()}


class FrameRegistry:
    def __init__(self, epoch, *, limit=128, ttl=300):
        self.epoch = epoch
        self.limit = limit
        self.ttl = ttl
        self._frames = OrderedDict()
        self._lock = threading.RLock()

    def add(self, target, geometry, *, revision=None):
        frame = Frame(f'f_{self.epoch}_{uuid.uuid4().hex}', target.owner,
                      target.target_id, target.revision if revision is None else revision, geometry)
        with self._lock:
            self._prune()
            self._frames[frame.frame_id] = frame
            while len(self._frames) > self.limit:
                self._frames.popitem(last=False)
        return frame

    def _prune(self):
        cutoff = time.monotonic() - self.ttl
        for key in [key for key, frame in self._frames.items() if frame.created < cutoff]:
            del self._frames[key]

    def get(self, owner, frame_id, target):
        with self._lock:
            self._prune()
            frame = self._frames.get(frame_id)
            if frame is None or frame.owner != owner or frame.target_id != target.target_id:
                raise RuntimeFault('FRAME_EXPIRED', 'Capture a new image for this target')
            if frame.revision != target.revision:
                raise RuntimeFault('FRAME_STALE', 'Target geometry or document changed. Capture a new image')
            return frame

    def release(self, owner):
        with self._lock:
            self._frames = OrderedDict((key, frame) for key, frame in self._frames.items() if frame.owner != owner)

    def discard(self, frame_id):
        with self._lock:
            self._frames.pop(frame_id, None)
