#!/usr/bin/env python3
# encoding: utf-8

__author__ = "ChenyangGao <https://chenyanggao.github.io>"
__all__ = ["AlistFuseOperations"]

try:
    # pip install cachetools
    from cachetools import Cache, LFUCache, TTLCache
    # pip install fusepy
    from fuse import FUSE, Operations, fuse_get_context
    # pip install psutil
    from psutil import Process
except ImportError:
    from subprocess import run
    from sys import executable
    run([executable, "-m", "pip", "install", "-U", "cachetools", "fusepy", "psutil"], check=True)
    from cachetools import Cache, LFUCache, TTLCache
    from fuse import FUSE, Operations, fuse_get_context # type: ignore
    from psutil import Process

import errno
import logging

from collections.abc import Callable, MutableMapping
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial, update_wrapper
from itertools import count
from posixpath import join as joinpath, split as splitpath
from stat import S_IFDIR, S_IFREG
from subprocess import run
from sys import maxsize
from threading import Lock, Thread
from time import sleep, time
from typing import cast, Any, BinaryIO, Concatenate, Final, ParamSpec
from unicodedata import normalize

from alist import AlistFileSystem, AlistPath

from .log import logger


Args = ParamSpec("Args")


def _get_process():
    pid = fuse_get_context()[-1]
    if pid <= 0:
        return "UNDETERMINED"
    return str(Process(pid))

PROCESS_STR = type("ProcessStr", (), {"__str__": staticmethod(_get_process)})()

if not hasattr(ThreadPoolExecutor, "__del__"):
    setattr(ThreadPoolExecutor, "__del__", lambda self, /: self.shutdown(cancel_futures=True))


def readdir_future_wrapper(
    self, 
    submit: Callable[Concatenate[Callable[Args, Any], Args], Future], 
    cooldown: int | float = 30, 
):
    readdir = type(self).readdir
    cooldown_pool: None | MutableMapping = None
    if cooldown > 0:
        cooldown_pool = TTLCache(maxsize, ttl=cooldown)
    task_pool: dict[str, Future] = {}
    pop_task = task_pool.pop
    lock = Lock()
    def wrapper(path, fh=0):
        path = normalize("NFC", path)
        refresh = cooldown_pool is None or path not in cooldown_pool
        try:
            result = [".", "..", *self._get_cache(path)]
        except KeyError:
            result = None
            refresh = True
        if refresh:
            with lock:
                try:
                    future = task_pool[path]
                except KeyError:
                    def done_callback(future: Future):
                        if cooldown_pool is not None and future.exception() is None:
                            cooldown_pool[path] = None
                        pop_task(path, None)
                    future = task_pool[path] = submit(readdir, self, path, fh)
                    future.add_done_callback(done_callback)
        if result is None:
            return future.result()
        else:
            return result
    return update_wrapper(wrapper, readdir)


# Learning: 
#   - https://www.stavros.io/posts/python-fuse-filesystem/
#   - https://thepythoncorner.com/posts/2017-02-27-writing-a-fuse-filesystem-in-python/
class AlistFuseOperations(Operations):

    def __init__(
        self, 
        /, 
        origin: str = "http://localhost:5244", 
        username: str = "", 
        password: str = "", 
        token: str = "", 
        base_dir: str = "/", 
        cache: None | MutableMapping = None, 
        max_readdir_workers: int = 5, 
        max_readdir_cooldown: float = 30, 
        predicate: None | Callable[[AlistPath], bool] = None, 
        strm_predicate: None | Callable[[AlistPath], bool] = None, 
        strm_make: None | Callable[[AlistPath], str] = None, 
        direct_open_names: None | Callable[[str], bool] = None, 
        direct_open_exes: None | Callable[[str], bool] = None, 
    ):
        self.__finalizer__: list[Callable] = []
        self._log = partial(logger.log, extra={"instance": repr(self)})

        self.fs = AlistFileSystem.login(origin, username, password)
        self.fs.chdir(base_dir)
        self.token = token
        self.predicate = predicate
        self.strm_predicate = strm_predicate
        self.strm_make = strm_make
        register = self.register_finalize = self.__finalizer__.append
        self.direct_open_names = direct_open_names
        self.direct_open_exes = direct_open_exes

        # NOTE: id generator for file handler
        self._next_fh: Callable[[], int] = count(1).__next__
        # NOTE: cache `readdir` pulled file attribute map
        if cache is None or isinstance(cache, (dict, Cache)):
            if cache is None:
                cache = {}
            self.temp_cache: None | MutableMapping = None
        else:
            self.temp_cache = LFUCache(128)
        self.cache: MutableMapping = cache
        self._fh_to_file: dict[int, tuple[BinaryIO, bytes]] = {}
        def close_all():
            popitem = self._fh_to_file.popitem
            while True:
                try:
                    _, (file, _) = popitem()
                    if file is not None:
                        file.close()
                except KeyError:
                    break
                except:
                    pass
        register(close_all)
        # NOTE: multi threaded directory reading control
        executor: None | ThreadPoolExecutor = None
        if max_readdir_workers == 0:
            executor = ThreadPoolExecutor(None)
            submit = executor.submit
        elif max_readdir_workers < 0:
            from concurrenttools import run_as_thread as submit
        else:
            executor = ThreadPoolExecutor(max_readdir_workers)
            submit = executor.submit
        self.__dict__["readdir"] = readdir_future_wrapper(
            self, 
            submit=submit, 
            cooldown=max_readdir_cooldown, 
        )
        if executor is not None:
            register(partial(executor.shutdown, wait=False, cancel_futures=True))
        self.normpath_map: dict[str, str] = {}

    def __del__(self, /):
        self.close()

    def close(self, /):
        for func in self.__finalizer__:
            try:
                func()
            except BaseException as e:
                self._log(logging.ERROR, "failed to finalize with %r", func)

    def getattr(self, /, path: str, fh: int = 0, _rootattr={"st_mode": S_IFDIR | 0o555}) -> dict:
        self._log(logging.DEBUG, "getattr(path=\x1b[4;34m%r\x1b[0m, fh=%r) by \x1b[3;4m%s\x1b[0m", path, fh, PROCESS_STR)
        if path == "/":
            return _rootattr
        dir_, name = splitpath(normalize("NFC", path))
        try:
            dird = self._get_cache(dir_)
        except KeyError:
            try:
                self.readdir(dir_)
                dird = self._get_cache(dir_)
            except BaseException as e:
                self._log(
                    logging.WARNING, 
                    "file not found: \x1b[4;34m%s\x1b[0m, since readdir failed: \x1b[4;34m%s\x1b[0m\n  |_ \x1b[1;4;31m%s\x1b[0m: %s", 
                    path, dir_, type(e).__qualname__, e, 
                )
                raise OSError(errno.EIO, path) from e
        try:
            return dird[name]
        except KeyError as e:
            self._log(
                logging.WARNING, 
                "file not found: \x1b[4;34m%s\x1b[0m\n  |_ \x1b[1;4;31m%s\x1b[0m: %s", 
                path, type(e).__qualname__, e, 
            )
            raise FileNotFoundError(errno.ENOENT, path) from e

    def open(self, /, path: str, flags: int = 0) -> int:
        self._log(logging.INFO, "open(path=\x1b[4;34m%r\x1b[0m, flags=%r) by \x1b[3;4m%s\x1b[0m", path, flags, PROCESS_STR)
        pid = fuse_get_context()[-1]
        path = self.normpath_map.get(normalize("NFC", path), path)
        if pid > 0:
            process = Process(pid)
            exe = process.exe()
            if (
                self.direct_open_names is not None and self.direct_open_names(process.name().lower()) or
                self.direct_open_exes is not None and self.direct_open_exes(exe)
            ):
                process.kill()
                def push():
                    sleep(.01)
                    run([exe, self.fs.get_url(path.lstrip("/"), token=self.token, ensure_ascii=False)])
                Thread(target=push).start()
                return 0
        return self._next_fh()

    def _open(self, path: str, /, start: int = 0):
        attr = self.getattr(path)
        path = self.normpath_map.get(normalize("NFC", path), path)
        if attr.get("_data") is not None:
            return None, attr["_data"]
        if attr["st_size"] <= 2048:
            return None, self.fs.as_path(path.lstrip("/")).read_bytes()
        file = cast(BinaryIO, self.fs.as_path(path.lstrip("/")).open("rb"))
        if start == 0:
            # cache 2048 in bytes (2 KB)
            preread = file.read(2048)
        else:
            preread = b""
        return file, preread

    def read(self, /, path: str, size: int, offset: int, fh: int = 0) -> bytes:
        self._log(logging.DEBUG, "read(path=\x1b[4;34m%r\x1b[0m, size=%r, offset=%r, fh=%r) by \x1b[3;4m%s\x1b[0m", path, size, offset, fh, PROCESS_STR)
        if not fh:
            return b""
        try:
            try:
                file, preread = self._fh_to_file[fh]
            except KeyError:
                file, preread = self._fh_to_file[fh] = self._open(path, offset)
            cache_size = len(preread)
            if offset < cache_size:
                if offset + size <= cache_size:
                    return preread[offset:offset+size]
                elif file is not None:
                    file.seek(cache_size)
                    return preread[offset:] + file.read(offset+size-cache_size)
            file.seek(offset)
            return file.read(size)
        except BaseException as e:
            self._log(
                logging.ERROR, 
                "can't read file: \x1b[4;34m%s\x1b[0m\n  |_ \x1b[1;4;31m%s\x1b[0m: %s", 
                path, type(e).__qualname__, e, 
            )
            raise OSError(errno.EIO, path) from e

    def _get_cache(self, path: str, /):
        if temp_cache := self.temp_cache:
            try:
                return temp_cache[path]
            except KeyError:
                value = temp_cache[path] = self.cache[path]
                return value
        return self.cache[path]

    def _set_cache(self, path: str, cache, /):
        if (temp_cache := self.temp_cache) is not None:
            temp_cache[path] = self.cache[path] = cache
        else:
            self.cache[path] = cache

    def readdir(self, /, path: str, fh: int = 0) -> list[str]:
        self._log(logging.DEBUG, "readdir(path=\x1b[4;34m%r\x1b[0m, fh=%r) by \x1b[3;4m%s\x1b[0m", path, fh, PROCESS_STR)
        predicate = self.predicate
        strm_predicate = self.strm_predicate
        strm_make = self.strm_make
        cache = {}
        path = normalize("NFC", path)
        realpath = self.normpath_map.get(path, path)
        try:
            ls = self.fs.listdir_path(realpath.lstrip("/"))
            for pathobj in ls:
                name    = pathobj.name
                subpath = pathobj.path
                isdir   = pathobj.is_dir()
                data = None
                if isdir:
                    size = 0
                if not isdir and strm_predicate and strm_predicate(pathobj):
                    if strm_make:
                        try:
                            url = strm_make(pathobj) or ""
                        except Exception:
                            url = ""
                        if not url:
                            self._log(
                                logging.WARNING, 
                                "can't make strm for file: \x1b[4;34m%s\x1b[0m", 
                                pathobj.relative_to(), 
                            )
                        data = url.encode("utf-8")
                    else:
                        data = pathobj.get_url(token=self.token, ensure_ascii=True).encode("utf-8")
                    size = len(cast(bytes, data))
                    name += ".strm"
                elif predicate and not predicate(pathobj):
                    continue
                else:
                    size = int(pathobj.get("size") or 0)
                normname = normalize("NFC", name)
                cache[normname] = dict(
                    st_mode=(S_IFDIR if isdir else S_IFREG) | 0o555, 
                    st_size=size, 
                    st_ctime=pathobj["ctime"], 
                    st_mtime=pathobj["mtime"], 
                    st_atime=pathobj.get("atime") or pathobj["mtime"], 
                    _data=data, 
                )
                normsubpath = joinpath(path, normname)
                if normsubpath != normalize("NFD", normsubpath):
                    self.normpath_map[normsubpath] = joinpath(realpath, name)
            self._set_cache(path, cache)
            return [".", "..", *cache]
        except BaseException as e:
            self._log(
                logging.ERROR, 
                "can't readdir: \x1b[4;34m%s\x1b[0m\n  |_ \x1b[1;4;31m%s\x1b[0m: %s", 
                path, type(e).__qualname__, e, 
            )
            raise OSError(errno.EIO, path) from e

    def release(self, /, path: str, fh: int = 0):
        self._log(logging.DEBUG, "release(path=\x1b[4;34m%r\x1b[0m, fh=%r) by \x1b[3;4m%s\x1b[0m", path, fh, PROCESS_STR)
        if not fh:
            return
        try:
            file, _ = self._fh_to_file.pop(fh)
            if file is not None:
                file.close()
        except KeyError:
            pass
        except BaseException as e:
            self._log(
                logging.ERROR, 
                "can't release file: \x1b[4;34m%s\x1b[0m\n  |_ \x1b[1;4;31m%s\x1b[0m: %s", 
                path, type(e).__qualname__, e, 
            )
            raise OSError(errno.EIO, path) from e

    def run(self, /, *args, **kwds):
        return FUSE(self, *args, **kwds)

