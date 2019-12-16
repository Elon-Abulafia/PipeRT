from sort_tracker.sort import Sort
import torch
from detectron2.structures import Instances, Boxes
import numpy as np
from base import BaseComponent
from queue import Queue, Full, Empty
import argparse
from urllib.parse import urlparse
import zerorpc
import gevent
import signal
import time
from core.routine_engine import RoutineMixin
from core.mini_logics import add_logic_to_thread, Metadata2Redis, MetadataFromRedis


class InstancesSort(Sort):

    def __init__(self, max_age: int = 1, min_hits: int = None, window_size: int = None, percent_seen: float = None,
                 verbose: bool = False):
        super().__init__(max_age, min_hits, window_size, percent_seen, verbose)

    def update_instances(self, instances: Instances):
        im_size = instances.image_size
        boxes = instances.get("pred_boxes").tensor.cpu().numpy()
        scores = instances.get("scores").cpu().unsqueeze(1).numpy()
        pred_classes = instances.get("pred_classes").cpu().unsqueeze(1).numpy()
        dets = np.concatenate((boxes, scores, pred_classes), axis=1)

        tracks = torch.tensor(self.update(dets))
        ret_tracks = Instances(im_size)
        ret_tracks.set("pred_boxes", Boxes(tracks[:, :4]))
        ret_tracks.set("scores", tracks[:, 4])
        ret_tracks.set("pred_classes", tracks[:, 5])
        ret_tracks.set("track_ids", tracks[:, -1])

        return ret_tracks


class SORTLogic(RoutineMixin):

    def __init__(self, stop_event, in_queue, out_queue, *args, **kwargs):
        super().__init__(stop_event, *args, **kwargs)
        self.in_queue = in_queue
        self.out_queue = out_queue
        self.sort = InstancesSort(*args, **kwargs)

    def main_logic(self, *args, **kwargs):
        try:
            instances = self.in_queue.get(block=False)
            new_instances = self.sort.update_instances(instances)
            try:
                self.out_queue.get(block=False)
                self.state.dropped += 1
            except Empty:
                pass
            self.out_queue.put(new_instances)
            return True
            # except Full:

                # return False

        except Empty:
            time.sleep(0)
            return False

    def setup(self, *args, **kwargs):
        pass

    def cleanup(self, *args, **kwargs):
        pass


class SORTComponent(BaseComponent):

    def __init__(self, in_key, out_key, redis_url, fps=30.0, maxlen=100):
        super().__init__(out_key, in_key)
        # TODO: should queue maxsize be configurable?
        self.in_queue = Queue(maxsize=1)
        self.out_queue = Queue(maxsize=1)
        MetadataFromRedis()
        t_get_meta_class = add_logic_to_thread(MetadataFromRedis)
        t_sort_class = add_logic_to_thread(SORTLogic)
        t_upload_meta_class = add_logic_to_thread(Metadata2Redis)

        t_get_meta = t_get_meta_class(self.stop_event, redis_url, in_key, self.in_queue)
        t_stream = t_stream_class(self.stop_event, self.queue, fps, name="capture_frame")
        t_upload = t_update_class(self.stop_event, out_key, redis_url, self.queue, maxlen, name="upload_redis")

        self.thread_list = [t_stream, t_upload]
        # for t in self.thread_list:
        #     t.add_event_handler(Events.BEFORE_LOGIC, tick)
        #     t.add_event_handler(Events.AFTER_LOGIC, tock)

        self._start()

    def _start(self):
        # start the thread to read frames from the video stream and upload to
        # redis

        for t in self.thread_list:
            t.daemon = True
            t.start()
        return self

    def _inner_stop(self):
        for t in self.thread_list:
            t.join()


