###############################################################################
#   lazyflow: data flow based lazy parallel computation framework
#
#       Copyright (C) 2011-2014, the ilastik developers
#                                <team@ilastik.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the Lesser GNU General Public License
# as published by the Free Software Foundation; either version 2.1
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# See the files LICENSE.lgpl2 and LICENSE.lgpl3 for full text of the
# GNU Lesser General Public License version 2.1 and 3 respectively.
# This information is also available on the ilastik web site at:
#		   http://ilastik.org/license/
###############################################################################

# Python
import gc
import threading
import weakref
import functools
import atexit


# lazyflow
from lazyflow.utility import OrderedSignal
from lazyflow.utility import Singleton
from lazyflow.utility import PriorityQueue
from lazyflow.utility import log_exception
from lazyflow.utility import Memory


import logging
logger = logging.getLogger(__name__)

default_refresh_interval = 5


class CacheMemoryManager(threading.Thread):
    """
    class for the management of cache memory

    The cache memory manager is a background thread that observes caches
    in use and cleans them up when the total memory consumption by the
    process exceeds the limit defined by the `lazyflow.utility.Memory`
    class. See the definition of the cache interfaces (opCache.py) to
    get an overview over the possible caches. 

    Usage:
    This manager is a singleton - just call its constructor somewhere
    and you will get a reference to the *only* running memory management
    thread.

    Interface:
    The manager provides a signal you can subscribe to
    >>> def writeFunction(x): print("total mem: {}".format(x))
    >>> mgr = CacheMemoryManager()
    >>> mgr.totalCacheMemory.subscribe(writeFunction)
    which emits the size of all observable caches, combined, in regular
    intervals.

    The update interval (for the signal and for automated cache release)
    can be set with a call to a class method
    >>> CacheMemoryManager().setRefreshInterval(5)
    the interval is measured in seconds. Each change of refresh interval
    triggers cleanup.
    """
    __metaclass__ = Singleton

    totalCacheMemory = OrderedSignal()

    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True

        self._caches = weakref.WeakSet()
        self._first_class_caches = weakref.WeakSet()
        self._observable_caches = weakref.WeakSet()
        self._managed_caches = weakref.WeakSet()
        self._managed_blocked_caches = weakref.WeakSet()

        self._condition = threading.Condition()
        self._disable_lock = threading.Condition()
        self._disabled = False
        self._refresh_interval = default_refresh_interval

        # maximum fraction of *allowed memory* used
        self._max_usage = 1.0
        # target usage fraction
        self._target_usage = .90

        self._stopped = False
        self.start()
        atexit.register(self.stop)

    def addFirstClassCache(self, cache):
        """
        add a first class cache (root cache) to the manager

        First class caches are handled differently so we are able to
        show a tree view of the caches (e.g. in ilastik). This method
        calls addCache() automatically.
        """
        # late import to prevent import loop
        from lazyflow.operators.opCache import Cache
        if isinstance(cache, Cache):
            self._first_class_caches.add(cache)
        self.addCache(cache)

    def getFirstClassCaches(self):
        """
        get a list of first class caches
        """
        return list(self._first_class_caches)

    def getCaches(self):
        """
        get a list of all caches (including first class caches)
        """
        return list(self._caches)

    def addCache(self, cache):
        """
        add a cache to be managed

        Caches are kept with weak references, so there is no need to
        remove them from the manager.
        """
        # late import to prevent import loop
        from lazyflow.operators.opCache import Cache
        from lazyflow.operators.opCache import ObservableCache
        from lazyflow.operators.opCache import ManagedCache
        from lazyflow.operators.opCache import ManagedBlockedCache
        assert isinstance(cache, Cache),\
            "Only Cache instances can be managed by CacheMemoryManager"
        self._caches.add(cache)
        if isinstance(cache, ObservableCache):
            self._observable_caches.add(cache)

        if isinstance(cache, ManagedBlockedCache):
            self._managed_blocked_caches.add(cache)
        elif isinstance(cache, ManagedCache):
            self._managed_caches.add(cache)

    def run(self):
        """
        main loop
        """
        while not self._stopped:
            self._wait()

            # acquire lock so that we don't get disabled during cleanup
            with self._disable_lock:
                if self._disabled or self._stopped:
                    continue
                self._cleanup()

    def _cleanup(self):
        """
        clean up once
        """
        from lazyflow.operators.opCache import ObservableCache
        try:
            # notify subscribed functions about current cache memory
            total = 0
            first_class_caches = self._first_class_caches.copy() # Avoid "RuntimeError: Set changed size during iteration"
            for cache in first_class_caches:
                if isinstance(cache, ObservableCache):
                    total += cache.usedMemory()
            self.totalCacheMemory(total)
            cache = None

            # check current memory state
            cache_memory = Memory.getAvailableRamCaches()

            if total <= self._max_usage * cache_memory:
                return

            # === we need a cache cleanup ===

            # queue holds time stamps and cleanup functions
            q = PriorityQueue()
            caches = list(self._managed_caches)
            for c in caches:
                q.push((c.lastAccessTime(), c.name, c.freeMemory))
            caches = list(self._managed_blocked_caches)
            for c in caches:
                for k, t in c.getBlockAccessTimes():
                    cleanupFun = functools.partial(c.freeBlock, k)
                    info = "{}: {}".format(c.name, k)
                    q.push((t, info, cleanupFun))
            c = None
            caches = None

            msg = "Caches are using {} memory".format(
                Memory.format(total))
            if cache_memory > 0:
                 msg += " ({:.1f}% of allowed)".format(
                    total*100.0/cache_memory)
            logger.debug(msg)

            while (total > self._target_usage * cache_memory
                   and len(q) > 0):
                t, info, cleanupFun = q.pop()
                mem = cleanupFun()
                logger.debug("Cleaned up {} ({})".format(
                    info, Memory.format(mem)))
                total -= mem
            gc.collect()
            # don't keep a reference until next loop iteration
            cleanupFun = None
            q = None

            msg = ("Done cleaning up, cache memory usage is now at "
                   "{}".format(Memory.format(total)))
            if cache_memory > 0:
                 msg += " ({:.1f}% of allowed)".format(
                    total*100.0/cache_memory)
            logger.debug(msg)
        except:
            log_exception(logger)

    def _wait(self):
        """
        sleep for _refresh_interval seconds or until woken up
        """
        with self._condition:
            self._condition.wait(self._refresh_interval)

    def stop(self):
        """
        Stop the memory manager thread in preparation for app exit.
        """
        self._stopped = True
        with self._condition:
            self._condition.notify()
        self.join()

    def setRefreshInterval(self, t):
        """
        set the clean up period and wake up the cleaning thread
        """
        with self._condition:
            self._refresh_interval = t
            self._condition.notifyAll()

    def disable(self):
        """
        disable all memory management

        This method blocks until current memory management tasks are finished.
        """
        with self._disable_lock:
            self._disabled = True

    def enable(self):
        """
        enable cache management and wake the thread
        """
        with self._disable_lock:
            self._disabled = False
        with self._condition:
            self._condition.notifyAll()