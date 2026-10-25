"""Bounded concurrent turns, keyed by fan. No queued duplicate turns or hidden retries."""
import asyncio
import os

def configured_fan_concurrency():
    raw=os.environ.get("FASTT_MAX_CONCURRENT_FANS","1")
    try:
        value=int(raw)
        if not 1<=value<=64:raise ValueError()
        return value
    except (TypeError,ValueError):
        raise ValueError("FASTT_MAX_CONCURRENT_FANS must be an integer from 1 to 64") from None

class FanTasks:
    def __init__(self,limit):
        if type(limit) is not int or not 1<=limit<=64:raise ValueError("invalid fan concurrency")
        self.limit=limit
        self.tasks={}

    def __contains__(self,fan):
        return fan in self.tasks

    def __len__(self):
        return len(self.tasks)

    def start(self,fan,factory):
        if fan in self.tasks or len(self.tasks)>=self.limit:return False
        self.tasks[fan]=asyncio.create_task(factory())
        return True

    def reap(self):
        errors=[]
        for fan,task in list(self.tasks.items()):
            if not task.done():continue
            del self.tasks[fan]
            try:task.result()
            except Exception as exc:errors.append(exc)
        if errors:raise errors[0]

    async def drain(self):
        """Keep in-flight provider outcomes intact; do not cancel and blindly replay them."""
        if self.tasks:
            await asyncio.gather(*self.tasks.values(),return_exceptions=True)
        self.reap()
