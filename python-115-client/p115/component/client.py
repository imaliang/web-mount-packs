#!/usr/bin/env python3
# encoding: utf-8

from __future__ import annotations

__author__ = "ChenyangGao <https://chenyanggao.github.io>"
__all__ = ["check_response", "P115Client", "ExportDirStatus", "PushExtractProgress", "ExtractProgress"]

import errno

from asyncio import to_thread
from base64 import b64encode
from binascii import b2a_hex
from collections.abc import (
    AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping, Sequence, 
)
from concurrent.futures import Future
from contextlib import asynccontextmanager
from datetime import date, datetime
from email.utils import formatdate
from functools import cached_property, partial, update_wrapper
from hashlib import md5, sha1, file_digest
from hmac import digest as hmac_digest
from http.cookiejar import Cookie
from http.cookies import Morsel
from inspect import isawaitable, iscoroutinefunction
from io import UnsupportedOperation
from itertools import count, takewhile
from json import dumps, loads
from os import fsdecode, fstat, stat, PathLike
from os import path as ospath
from re import compile as re_compile
from time import strftime, strptime, time
from typing import (
    cast, overload, Any, Final, Literal, Never, Optional, Self, TypeVar, 
)
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4
from xml.etree.ElementTree import fromstring

from asynctools import as_thread
from cookietools import cookies_str_to_dict, create_cookie
from filewrap import (
    SupportsRead, 
    bio_chunk_iter, bio_chunk_async_iter, 
    bio_skip_iter, bio_skip_async_iter, 
    bytes_iter_skip, bytes_async_iter_skip, 
    bytes_to_chunk_iter, bytes_to_chunk_async_iter, 
    bytes_ensure_part_iter, bytes_ensure_part_async_iter, 
)
from http_request import encode_multipart_data, encode_multipart_data_async, SupportsGeturl
from http_response import get_content_length, get_filename, get_total_length, is_range_request
from httpfile import RequestsFileReader # TODO: use urllib3 instead
from httpx import AsyncClient, Client, Cookies, TimeoutException
from httpx_request import request
from iterutils import through, async_through, wrap_iter, wrap_aiter
from multidict import CIMultiDict
from qrcode import QRCode
from startfile import startfile, startfile_async
from urlopen import urlopen
from yarl import URL

from .cipher import P115RSACipher, P115ECDHCipher, MD5_SALT
from .exception import AuthenticationError, LoginError


RequestVarT = TypeVar("RequestVarT", dict, Callable)
RSA_ENCODER: Final = P115RSACipher()
ECDH_ENCODER: Final = P115ECDHCipher()
CRE_SHARE_LINK_search = re_compile(r"/s/(?P<share_code>\w+)(\?password=(?P<receive_code>\w+))?").search
APP_VERSION: Final = "99.99.99.99"


request = partial(request, parse=lambda _, content: loads(content))


def to_base64(s: bytes | str, /) -> str:
    if isinstance(s, str):
        s = bytes(s, "utf-8")
    return str(b64encode(s), "ascii")


def check_response(fn: RequestVarT, /) -> RequestVarT:
    """检测 115 的某个接口的响应，如果成功则直接返回，否则根据具体情况抛出一个异常
    """
    def check(resp):
        if not isinstance(resp, dict):
            raise TypeError("the response should be dict")
        if resp.get("state", True):
            return resp
        if "errno" in resp:
            match resp["errno"]:
                # {"state": false, "errno": 99, "error": "请重新登录", "request": "/app/uploadinfo", "data": []}
                case 99:
                    raise AuthenticationError(resp)
                # {"state": false, "errno": 911, "errcode": 911, "error_msg": "请验证账号"}
                case 911:
                    raise AuthenticationError(resp)
                # {"state": false, "errno": 20004, "error": "该目录名称已存在。", "errtype": "war"}
                case 20004:
                    raise FileExistsError(resp)
                # {"state": false, "errno": 20009, "error": "父目录不存在。", "errtype": "war"}
                case 20009:
                    raise FileNotFoundError(resp)
                # {"state": false, "errno": 91002, "error": "不能将文件复制到自身或其子目录下。", "errtype": "war"}
                case 91002:
                    raise OSError(errno.ENOTSUP, resp)
                # {"state": false, "errno": 91004, "error": "操作的文件(夹)数量超过5万个", "errtype": "war"}
                case 91004:
                    raise OSError(errno.ENOTSUP, resp)
                # {"state": false, "errno": 91005, "error": "空间不足，复制失败。", "errtype": "war"}
                case 91005:
                    raise OSError(errno.ENOSPC, resp)
                # {"state": false, "errno": 90008, "error": "文件（夹）不存在或已经删除。", "errtype": "war"}
                case 90008:
                    raise FileNotFoundError(resp)
                # {"state": false,  "errno": 231011, "error": "文件已删除，请勿重复操作","errtype": "war"}
                case 231011:
                    raise FileNotFoundError(resp)
                # {"state": false, "errno": 990009, "error": "删除[...]操作尚未执行完成，请稍后再试！", "errtype": "war"}
                # {"state": false, "errno": 990009, "error": "还原[...]操作尚未执行完成，请稍后再试！", "errtype": "war"}
                # {"state": false, "errno": 990009, "error": "复制[...]操作尚未执行完成，请稍后再试！", "errtype": "war"}
                # {"state": false, "errno": 990009, "error": "移动[...]操作尚未执行完成，请稍后再试！", "errtype": "war"}
                case 990009:
                    raise OSError(errno.EBUSY, resp)
                # {"state": false, "errno": 990023, "error": "操作的文件(夹)数量超过5万个", "errtype": ""}
                case 990023:
                    raise OSError(errno.ENOTSUP, resp)
                # {"state": 0, "errno": 40100000, "code": 40100000, "data": {}, "message": "参数错误！", "error": "参数错误！"}
                case 40100000:
                    raise OSError(errno.EINVAL, resp)
                # {"state": 0, "errno": 40101032, "code": 40101032, "data": {}, "message": "请重新登录", "error": "请重新登录"}
                case 40101032:
                    raise AuthenticationError(resp)
        elif "errNo" in resp:
            match resp["errNo"]:
                case 990001:
                    raise AuthenticationError(resp)
        raise OSError(errno.EIO, resp)
    if isinstance(fn, dict):
        return check(fn)
    elif iscoroutinefunction(fn):
        async def wrapper(*args, **kwds):
            return check(await fn(*args, **kwds))
    elif callable(fn):
        def wrapper(*args, **kwds):
            return check(fn(*args, **kwds))
    else:
        raise TypeError("the response should be dict")
    return update_wrapper(wrapper, fn)


class UrlStr(str):

    def __new__(cls, url="", /, *args, **kwds):
        return super().__new__(cls, url)

    def __init__(self, url="", /, *args, **kwds):
        self.__dict__.update(*args, **kwds)

    def __delattr__(self, attr, /) -> Never:
        raise TypeError("can't delete attributes")

    def __getitem__(self, key, /):
        return self.__dict__[key]

    def __setattr__(self, attr, val, /) -> Never:
        raise TypeError("can't set attributes")

    def __repr__(self, /) -> str:
        return f"{type(self).__qualname__}({str(self)!r}, {self.__dict__})"

    def geturl(self) -> str:
        return str(self)


class P115Client:
    """115 的客户端对象
    :param cookies: 115 的 cookies，要包含 UID、CID 和 SEID，如果为 None，则会要求人工扫二维码登录
    :param app: 人工扫二维码后绑定的 app
    :param open_qrcode_on_console: 在命令行输出二维码，否则在浏览器中打开

    设备列表如下：

    | No.    | ssoent  | app        | description            |
    |-------:|:--------|:-----------|:-----------------------|
    |      1 | A1      | web        | 网页版                 |
    |      2 | A2      | ?          | 未知: android          |
    |      3 | A3      | ?          | 未知: iphone           |
    |      4 | A4      | ?          | 未知: ipad             |
    |      5 | B1      | ?          | 未知: android          |
    |      6 | D1      | ios        | 115生活(iOS端)         |
    |      7 | F1      | android    | 115生活(Android端)     |
    |      8 | H1      | ?          | 未知: ipad             |
    |      9 | I1      | tv         | 115网盘(Android电视端) |
    |     10 | M1      | qandriod   | 115管理(Android端)     |
    |     11 | N1      | ?          | 115管理(iOS端)         |
    |     12 | O1      | ?          | 未知: ipad             |
    |     13 | P1      | windows    | 115生活(Windows端)     |
    |     14 | P2      | mac        | 115生活(macOS端)       |
    |     15 | P3      | linux      | 115生活(Linux端)       |
    |     16 | R1      | wechatmini | 115生活(微信小程序)    |
    |     17 | R2      | alipaymini | 115生活(支付宝小程序)  |
    """
    def __init__(
        self, 
        /, 
        cookies: None | str | Mapping[str, str] | Cookies | Iterable[Mapping | Cookie | Morsel] = None, 
        app: str = "web", 
        open_qrcode_on_console: bool = True, 
    ):
        self.__dict__.update(
            headers = CIMultiDict({
                "Accept": "application/json, text/plain, */*", 
                "Accept-Encoding": "gzip, deflate", 
                "Connection": "keep-alive", 
                "User-Agent": "Mozilla/5.0 AppleWebKit/600 Safari/600 Chrome/124.0.0.0 115disk/" + APP_VERSION, 
            }), 
            cookies = Cookies(), 
        )
        if cookies is None:
            resp = self.login_with_qrcode(app, open_qrcode_on_console=open_qrcode_on_console)
            cookies = resp["data"]["cookie"]
        if cookies:
            self.cookies = cookies
            upload_info = self.upload_info
            if not upload_info["state"]:
                raise AuthenticationError(upload_info)

    def __del__(self, /):
        self.close()

    def __eq__(self, other, /) -> bool:
        try:
            return type(self) is type(other) and self.user_id == other.user_id
        except AttributeError:
            return False

    @cached_property
    def session(self, /) -> Client:
        """同步请求的 session
        """
        ns = self.__dict__
        session = Client()
        session._headers = ns["headers"]
        session._cookies = ns["cookies"]
        return session

    @cached_property
    def async_session(self, /) -> AsyncClient:
        """异步请求的 session
        """
        ns = self.__dict__
        session = AsyncClient()
        session._headers = ns["headers"]
        session._cookies = ns["cookies"]
        return session

    @property
    def cookies(self, /) -> str:
        """115 登录的 cookies，包含 UID, CID 和 SEID 这 3 个字段
        """
        cookies = self.__dict__["cookies"]
        return "; ".join(f"{key}={cookies.get(key, '')}" for key in ("UID", "CID", "SEID"))

    @cookies.setter
    def cookies(self, cookies: str | Mapping[str, str] | Cookies | Iterable[Mapping | Cookie | Morsel], /):
        """更新 cookies
        """
        if isinstance(cookies, str):
            cookies = cookies_str_to_dict(cookies.strip())
        set_cookie = self.__dict__["cookies"].jar.set_cookie
        if isinstance(cookies, Mapping):
            for key in cookies:
                set_cookie(create_cookie(key, cookies[key], domain=".115.com"))
        else:
            if isinstance(cookies, Cookies):
                cookies = cookies.jar
            for cookie in cookies:
                set_cookie(create_cookie("", cookie))
        self.__dict__.pop("upload_info", None)

    @property
    def headers(self, /) -> CIMultiDict:
        """请求头，无论同步还是异步请求都共用这个请求头
        """
        return self.__dict__["headers"]

    @headers.setter
    def headers(self, headers, /):
        """替换请求头，如果需要更新，请用 <client>.headers.update
        """
        headers = CIMultiDict(headers)
        default_headers = self.headers
        default_headers.clear()
        default_headers.update(headers)

    def close(self, /) -> None:
        """删除 session 和 async_session，如果它们未被引用，则会被自动清理
        """
        ns = self.__dict__
        ns.pop("session", None)
        ns.pop("async_session", None)

    def request(
        self, 
        /, 
        url: str, 
        method: str = "GET", 
        async_: bool = False, 
        session = None, 
        **request_kwargs, 
    ):
        """帮助函数：可执行同步和异步的网络请求
        """
        if session is None:
            session = self.async_session if async_ else self.session
        return request(
            url, 
            method, 
            async_=async_, 
            session=session, 
            **request_kwargs, 
        )

    ########## Login API ##########

    def login_status(
        self, 
        /, 
        **request_kwargs, 
    ) -> bool:
        """检查是否已登录
        GET https://my.115.com/?ct=guide&ac=status
        """
        api = "https://my.115.com/?ct=guide&ac=status"
        def parse(resp, content: bytes) -> bool:
            try:
                return loads(content)["state"]
            except:
                return False
        request_kwargs["parse"] = parse
        return self.request(api, **request_kwargs)

    def login_check(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """检查当前用户的登录状态
        GET https://passportapi.115.com/app/1.0/web/1.0/check/sso
        """
        api = "https://passportapi.115.com/app/1.0/web/1.0/check/sso"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def login_device(
        self, 
        /, 
        **request_kwargs, 
    ) -> None | dict:
        """获取当前的登录设备的信息，如果为 None，则说明登录失效
        """
        def parse(resp, content: bytes) -> None | dict:
            login_devices = loads(content)
            if not login_devices["state"]:
                return None
            return next(d for d in login_devices["data"]["list"] if d["is_current"])
        request_kwargs["parse"] = parse
        return self.login_devices(**request_kwargs)

    def login_devices(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取所有的已登录设备的信息，不过当前必须未登录失效
        GET https://passportapi.115.com/app/1.0/web/9.2/login_log/login_devices
        """
        api = "https://passportapi.115.com/app/1.0/web/9.2/login_log/login_devices"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def login(
        self, 
        /, 
        app: str = "web", 
        open_qrcode_on_console: bool = True, 
        **request_kwargs, 
    ):
        """扫码二维码登录，如果已登录则忽略
        app 共有 17 个可用值，目前找出 10 个：
            - web
            - ios
            - android
            - tv
            - qandroid
            - windows
            - mac
            - linux
            - wechatmini
            - alipaymini

        设备列表如下：

        | No.    | ssoent  | app        | description            |
        |-------:|:--------|:-----------|:-----------------------|
        |      1 | A1      | web        | 网页版                 |
        |      2 | A2      | ?          | 未知: android          |
        |      3 | A3      | ?          | 未知: iphone           |
        |      4 | A4      | ?          | 未知: ipad             |
        |      5 | B1      | ?          | 未知: android          |
        |      6 | D1      | ios        | 115生活(iOS端)         |
        |      7 | F1      | android    | 115生活(Android端)     |
        |      8 | H1      | ?          | 未知: ipad             |
        |      9 | I1      | tv         | 115网盘(Android电视端) |
        |     10 | M1      | qandriod   | 115管理(Android端)     |
        |     11 | N1      | ?          | 115管理(iOS端)         |
        |     12 | O1      | ?          | 未知: ipad             |
        |     13 | P1      | windows    | 115生活(Windows端)     |
        |     14 | P2      | mac        | 115生活(macOS端)       |
        |     15 | P3      | linux      | 115生活(Linux端)       |
        |     16 | R1      | wechatmini | 115生活(微信小程序)    |
        |     17 | R2      | alipaymini | 115生活(支付宝小程序)  |
        """
        async_ = request_kwargs.get("async_", False)
        if async_:
            async def async_request():
                if not (await self.login_status(**request_kwargs)):
                    self.cookies = (await self.login_with_qrcode(
                        app, 
                        open_qrcode_on_console=open_qrcode_on_console, 
                        **request_kwargs, 
                    ))["data"]["cookie"]
            return async_request()
        else:
            if not self.login_status(**request_kwargs):
                self.cookies = self.login_with_qrcode(
                    app, open_qrcode_on_console=open_qrcode_on_console, **request_kwargs, 
                )["data"]["cookie"]

    @classmethod
    def login_with_qrcode(
        cls, 
        /, 
        app: str = "web", 
        open_qrcode_on_console: bool = True, 
        **request_kwargs, 
    ) -> dict:
        """扫码二维码登录，获取响应（如果需要更新此 client 的 cookies，请直接用 login 方法）
        app 共有 17 个可用值，目前找出 10 个：
            - web
            - ios
            - android
            - tv
            - qandroid
            - windows
            - mac
            - linux
            - wechatmini
            - alipaymini

        设备列表如下：

        | No.    | ssoent  | app        | description            |
        |-------:|:--------|:-----------|:-----------------------|
        |      1 | A1      | web        | 网页版                 |
        |      2 | A2      | ?          | 未知: android          |
        |      3 | A3      | ?          | 未知: iphone           |
        |      4 | A4      | ?          | 未知: ipad             |
        |      5 | B1      | ?          | 未知: android          |
        |      6 | D1      | ios        | 115生活(iOS端)         |
        |      7 | F1      | android    | 115生活(Android端)     |
        |      8 | H1      | ?          | 未知: ipad             |
        |      9 | I1      | tv         | 115网盘(Android电视端) |
        |     10 | M1      | qandriod   | 115管理(Android端)     |
        |     11 | N1      | ?          | 115管理(iOS端)         |
        |     12 | O1      | ?          | 未知: ipad             |
        |     13 | P1      | windows    | 115生活(Windows端)     |
        |     14 | P2      | mac        | 115生活(macOS端)       |
        |     15 | P3      | linux      | 115生活(Linux端)       |
        |     16 | R1      | wechatmini | 115生活(微信小程序)    |
        |     17 | R2      | alipaymini | 115生活(支付宝小程序)  |
        """
        async_ = request_kwargs.get("async_", False)
        if async_:
            async def async_request():
                qrcode_token = (await cls.login_qrcode_token(**request_kwargs))["data"]
                qrcode = qrcode_token.pop("qrcode")
                if open_qrcode_on_console:
                    qr = QRCode(border=1)
                    qr.add_data(qrcode)
                    qr.print_ascii(tty=True)
                else:
                    await startfile_async("https://qrcodeapi.115.com/api/1.0/mac/1.0/qrcode?uid=" + qrcode_token["uid"])
                while True:
                    try:
                        resp = await cls.login_qrcode_status(qrcode_token, **request_kwargs)
                    except TimeoutException:
                        continue
                    status = resp["data"].get("status")
                    if status == 0:
                        print("[status=0] qrcode: waiting")
                    elif status == 1:
                        print("[status=1] qrcode: scanned")
                    elif status == 2:
                        print("[status=2] qrcode: signed in")
                        break
                    elif status == -1:
                        raise LoginError("[status=-1] qrcode: expired")
                    elif status == -2:
                        raise LoginError("[status=-2] qrcode: canceled")
                    else:
                        raise LoginError(f"qrcode: aborted with {resp!r}")
                return await cls.login_qrcode_result({"account": qrcode_token["uid"], "app": app}, **request_kwargs)
            return async_request()
        else:
            qrcode_token = cls.login_qrcode_token(**request_kwargs)["data"]
            qrcode = qrcode_token.pop("qrcode")
            if open_qrcode_on_console:
                qr = QRCode(border=1)
                qr.add_data(qrcode)
                qr.print_ascii(tty=True)
            else:
                startfile("https://qrcodeapi.115.com/api/1.0/mac/1.0/qrcode?uid=" + qrcode_token["uid"])
            while True:
                try:
                    resp = cls.login_qrcode_status(qrcode_token, **request_kwargs)
                except TimeoutException:
                    continue
                status = resp["data"].get("status")
                if status == 0:
                    print("[status=0] qrcode: waiting")
                elif status == 1:
                    print("[status=1] qrcode: scanned")
                elif status == 2:
                    print("[status=2] qrcode: signed in")
                    break
                elif status == -1:
                    raise LoginError("[status=-1] qrcode: expired")
                elif status == -2:
                    raise LoginError("[status=-2] qrcode: canceled")
                else:
                    raise LoginError(f"qrcode: aborted with {resp!r}")
            return cls.login_qrcode_result({"account": qrcode_token["uid"], "app": app}, **request_kwargs)

    def login_another_app(
        self, 
        /, 
        app: str = "web", 
        replace: bool = False, 
        **request_kwargs, 
    ) -> Self:
        """登录某个设备（同一个设备最多同时一个在线，即最近登录的那个）
        :param app: 要登录的 app
        :param replace: 替换当前 client 对象的 cookie，否则返回新的 client 对象

        设备列表如下：

        | No.    | ssoent  | app        | description            |
        |-------:|:--------|:-----------|:-----------------------|
        |      1 | A1      | web        | 网页版                 |
        |      2 | A2      | ?          | 未知: android          |
        |      3 | A3      | ?          | 未知: iphone           |
        |      4 | A4      | ?          | 未知: ipad             |
        |      5 | B1      | ?          | 未知: android          |
        |      6 | D1      | ios        | 115生活(iOS端)         |
        |      7 | F1      | android    | 115生活(Android端)     |
        |      8 | H1      | ?          | 未知: ipad             |
        |      9 | I1      | tv         | 115网盘(Android电视端) |
        |     10 | M1      | qandriod   | 115管理(Android端)     |
        |     11 | N1      | ?          | 115管理(iOS端)         |
        |     12 | O1      | ?          | 未知: ipad             |
        |     13 | P1      | windows    | 115生活(Windows端)     |
        |     14 | P2      | mac        | 115生活(macOS端)       |
        |     15 | P3      | linux      | 115生活(Linux端)       |
        |     16 | R1      | wechatmini | 115生活(微信小程序)    |
        |     17 | R2      | alipaymini | 115生活(支付宝小程序)  |
        """
        async_ = request_kwargs.get("async_", False)
        if async_:
            async def async_request():
                uid = check_response(await self.login_qrcode_token(**request_kwargs))["data"]["uid"]
                check_response(await self.login_qrcode_scan(uid, **request_kwargs))
                check_response(await self.login_qrcode_scan_confirm(uid, **request_kwargs))
                data = check_response(await self.login_qrcode_result({"account": uid, "app": app}, **request_kwargs))
                if replace:
                    self.cookies = data["data"]["cookie"]
                    return self
                else:
                    return type(self)(data["data"]["cookie"])
            return async_request()
        else:
            uid = check_response(self.login_qrcode_token(**request_kwargs))["data"]["uid"]
            check_response(self.login_qrcode_scan(uid, **request_kwargs))
            check_response(self.login_qrcode_scan_confirm(uid, **request_kwargs))
            data = check_response(self.login_qrcode_result({"account": uid, "app": app}, **request_kwargs))
            if replace:
                self.cookies = data["data"]["cookie"]
                return self
            else:
                return type(self)(data["data"]["cookie"])

    @staticmethod
    def login_qrcode_scan(
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """扫描二维码，payload 数据取自 `login_qrcode_token` 接口响应
        GET https://qrcodeapi.115.com/api/2.0/prompt.php
        payload:
            - uid: str
        """
        api = "https://qrcodeapi.115.com/api/2.0/prompt.php"
        if isinstance(payload, str):
            payload = {"uid": payload}
        request_kwargs.pop("parse", None)
        return request(api, params=payload, **request_kwargs)

    @staticmethod
    def login_qrcode_scan_confirm(
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """确认扫描二维码，payload 数据取自 `login_qrcode_scan` 接口响应
        GET https://hnqrcodeapi.115.com/api/2.0/slogin.php
        payload:
            - key: str
            - uid: str
            - client: int = 0
        """
        api = "https://hnqrcodeapi.115.com/api/2.0/slogin.php"
        if isinstance(payload, str):
            payload = {"key": payload, "uid": payload, "client": 0}
        request_kwargs.pop("parse", None)
        return request(api, params=payload, **request_kwargs)

    @staticmethod
    def login_qrcode_scan_cancel(
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """确认扫描二维码，payload 数据取自 `login_qrcode_scan` 接口响应
        GET https://hnqrcodeapi.115.com/api/2.0/cancel.php
        payload:
            - key: str
            - uid: str
            - client: int = 0
        """
        api = "https://hnqrcodeapi.115.com/api/2.0/cancel.php"
        if isinstance(payload, str):
            payload = {"key": payload, "uid": payload, "client": 0}
        request_kwargs.pop("parse", None)
        return request(api, params=payload, **request_kwargs)

    @staticmethod
    def login_qrcode_status(
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取二维码的状态（未扫描、已扫描、已登录、已取消、已过期等），payload 数据取自 `login_qrcode_token` 接口响应
        GET https://qrcodeapi.115.com/get/status/
        payload:
            - uid: str
            - time: int
            - sign: str
        """
        api = "https://qrcodeapi.115.com/get/status/"
        request_kwargs.pop("parse", None)
        return request(api, params=payload, **request_kwargs)

    @staticmethod
    def login_qrcode_result(
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取扫码登录的结果，包含 cookie
        POST https://passportapi.115.com/app/1.0/{app}/1.0/login/qrcode/
        payload:
            - account: int | str
            - app: str = "web"
        """
        if isinstance(payload, (int, str)):
            payload = {"app": "web", "account": payload}
        else:
            payload = {"app": "web", **payload}
        api = f"https://passportapi.115.com/app/1.0/{payload['app']}/1.0/login/qrcode/"
        request_kwargs.pop("parse", None)
        return request(api, "POST", data=payload, **request_kwargs)

    @staticmethod
    def login_qrcode_token(**request_kwargs) -> dict:
        """获取登录二维码，扫码可用
        GET https://qrcodeapi.115.com/api/1.0/web/1.0/token/
        """
        api = "https://qrcodeapi.115.com/api/1.0/web/1.0/token/"
        request_kwargs.pop("parse", None)
        return request(api, **request_kwargs)

    @staticmethod
    def login_qrcode(
        uid: str, 
        **request_kwargs, 
    ) -> bytes:
        """下载登录二维码图片（PNG）
        GET https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode
        :params uid: 二维码的 uid
        :return: `requests.Response` 或 `aiohttp.ClientResponse`
        """
        api = "https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode"
        request_kwargs["params"] = {"uid": uid}
        request_kwargs["parse"] = False
        return request(api, **request_kwargs)

    def logout(
        self, 
        /, 
        **request_kwargs, 
    ):
        """退出当前设备的登录状态
        """
        async_ = request_kwargs.get("async_", False)
        if async_:
            async def async_request():
                login_devices = await self.login_devices(**request_kwargs)
                if login_devices["state"]:
                    current_device = next(d for d in login_devices["data"]["list"] if d["is_current"])
                    await self.logout_by_ssoent(current_device["ssoent"], **request_kwargs)
            return async_request()
        else:
            login_devices = self.login_devices(**request_kwargs)
            if login_devices["state"]:
                current_device = next(d for d in login_devices["data"]["list"] if d["is_current"])
                self.logout_by_ssoent(current_device["ssoent"], **request_kwargs)

    def logout_by_app(
        self, 
        /, 
        app: str,  
        **request_kwargs, 
    ):
        """退出登录状态（可以把某个客户端下线，所有已登录设备可从 `login_devices` 获取）
        GET https://passportapi.115.com/app/1.0/{app}/1.0/logout/logout

        :param app: 退出登录的 app

        设备列表如下：

        | No.    | ssoent  | app        | description            |
        |-------:|:--------|:-----------|:-----------------------|
        |      1 | A1      | web        | 网页版                 |
        |      2 | A2      | ?          | 未知: android          |
        |      3 | A3      | ?          | 未知: iphone           |
        |      4 | A4      | ?          | 未知: ipad             |
        |      5 | B1      | ?          | 未知: android          |
        |      6 | D1      | ios        | 115生活(iOS端)         |
        |      7 | F1      | android    | 115生活(Android端)     |
        |      8 | H1      | ?          | 未知: ipad             |
        |      9 | I1      | tv         | 115网盘(Android电视端) |
        |     10 | M1      | qandriod   | 115管理(Android端)     |
        |     11 | N1      | ?          | 115管理(iOS端)         |
        |     12 | O1      | ?          | 未知: ipad             |
        |     13 | P1      | windows    | 115生活(Windows端)     |
        |     14 | P2      | mac        | 115生活(macOS端)       |
        |     15 | P3      | linux      | 115生活(Linux端)       |
        |     16 | R1      | wechatmini | 115生活(微信小程序)    |
        |     17 | R2      | alipaymini | 115生活(支付宝小程序)  |
        """
        api = f"https://passportapi.115.com/app/1.0/{app}/1.0/logout/logout"
        request_kwargs["headers"] = {**(request_kwargs.get("headers") or {}), "Cookie": self.cookies}
        request_kwargs.setdefault("parse", None)
        return request(api, **request_kwargs)

    def logout_by_ssoent(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """退出登录状态（可以把某个客户端下线，所有已登录设备可从 `login_devices` 获取）
        GET https://passportapi.115.com/app/1.0/web/1.0/logout/mange
        payload:
            ssoent: str

        设备列表如下：

        | No.    | ssoent  | app        | description            |
        |-------:|:--------|:-----------|:-----------------------|
        |      1 | A1      | web        | 网页版                 |
        |      2 | A2      | ?          | 未知: android          |
        |      3 | A3      | ?          | 未知: iphone           |
        |      4 | A4      | ?          | 未知: ipad             |
        |      5 | B1      | ?          | 未知: android          |
        |      6 | D1      | ios        | 115生活(iOS端)         |
        |      7 | F1      | android    | 115生活(Android端)     |
        |      8 | H1      | ?          | 未知: ipad             |
        |      9 | I1      | tv         | 115网盘(Android电视端) |
        |     10 | M1      | qandriod   | 115管理(Android端)     |
        |     11 | N1      | ?          | 115管理(iOS端)         |
        |     12 | O1      | ?          | 未知: ipad             |
        |     13 | P1      | windows    | 115生活(Windows端)     |
        |     14 | P2      | mac        | 115生活(macOS端)       |
        |     15 | P3      | linux      | 115生活(Linux端)       |
        |     16 | R1      | wechatmini | 115生活(微信小程序)    |
        |     17 | R2      | alipaymini | 115生活(支付宝小程序)  |
        """
        api = "https://passportapi.115.com/app/1.0/web/1.0/logout/mange"
        if isinstance(payload, str):
            payload = {"ssoent": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    ########## Account API ##########

    def user_info(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取此用户信息
        GET https://my.115.com/?ct=ajax&ac=nav
        """
        api = "https://my.115.com/?ct=ajax&ac=nav"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def user_info2(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取此用户信息（更全）
        GET https://my.115.com/?ct=ajax&ac=get_user_aq
        """
        api = "https://my.115.com/?ct=ajax&ac=get_user_aq"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def user_setting(
        self, 
        payload: dict = {}, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取（并可修改）此账户的网页版设置（提示：较为复杂，自己抓包研究）
        POST https://115.com/?ac=setting&even=saveedit&is_wl_tpl=1
        """
        api = "https://115.com/?ac=setting&even=saveedit&is_wl_tpl=1"
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    ########## App API ##########

    @staticmethod
    def app_version_list(**request_kwargs) -> dict:
        """获取当前各平台最新版 115 app 下载链接
        GET https://appversion.115.com/1/web/1.0/api/chrome
        """
        api = "https://appversion.115.com/1/web/1.0/api/chrome"
        request_kwargs.pop("parse", None)
        return request(api, **request_kwargs)

    ########## File System API ##########

    def fs_space_summury(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取数据报告
        POST https://webapi.115.com/user/space_summury
        """
        api = "https://webapi.115.com/user/space_summury"
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", **request_kwargs)

    def fs_batch_copy(
        self, 
        payload: dict | Iterable[int | str], 
        /, 
        pid: int = 0, 
        **request_kwargs, 
    ) -> dict:
        """复制文件或文件夹
        POST https://webapi.115.com/files/copy
        payload:
            - pid: int | str
            - fid[0]: int | str
            - fid[1]: int | str
            - ...
        """
        api = "https://webapi.115.com/files/copy"
        if isinstance(payload, dict):
            payload = {"pid": pid, **payload}
        else:
            payload = {f"fid[{fid}]": fid for i, fid in enumerate(payload)}
            if not payload:
                return {"state": False, "message": "no op"}
            payload["pid"] = pid
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_batch_delete(
        self, 
        payload: dict | Iterable[int | str], 
        /, 
        **request_kwargs, 
    ) -> dict:
        """删除文件或文件夹
        POST https://webapi.115.com/rb/delete
        payload:
            - fid[0]: int | str
            - fid[1]: int | str
            - ...
        """
        api = "https://webapi.115.com/rb/delete"
        if not isinstance(payload, dict):
            payload = {f"fid[{i}]": fid for i, fid in enumerate(payload)}
        if not payload:
            return {"state": False, "message": "no op"}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_batch_move(
        self, 
        payload: dict | Iterable[int | str], 
        /, 
        pid: int = 0, 
        **request_kwargs, 
    ) -> dict:
        """移动文件或文件夹
        POST https://webapi.115.com/files/move
        payload:
            - pid: int | str
            - fid[0]: int | str
            - fid[1]: int | str
            - ...
        """
        api = "https://webapi.115.com/files/move"
        if isinstance(payload, dict):
            payload = {"pid": pid, **payload}
        else:
            payload = {f"fid[{i}]": fid for i, fid in enumerate(payload)}
            if not payload:
                return {"state": False, "message": "no op"}
            payload["pid"] = pid
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_batch_rename(
        self, 
        payload: dict | Iterable[tuple[int | str, str]], 
        /, 
        **request_kwargs, 
    ) -> dict:
        """重命名文件或文件夹
        POST https://webapi.115.com/files/batch_rename
        payload:
            - files_new_name[{file_id}]: str # 值为新的文件名（basename）
        """
        api = "https://webapi.115.com/files/batch_rename"
        if not isinstance(payload, dict):
            payload = {f"files_new_name[{fid}]": name for fid, name in payload}
        if not payload:
            return {"state": False, "message": "no op"}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_copy(
        self, 
        id: int | str, 
        /, 
        pid: int = 0, 
        **request_kwargs, 
    ) -> dict:
        """复制文件或文件夹，此接口是对 `fs_batch_copy` 的封装
        """
        return self.fs_batch_copy({"fid[0]": id, "pid": pid}, **request_kwargs)

    def fs_delete(
        self, 
        id: int | str, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """删除文件或文件夹，此接口是对 `fs_batch_delete` 的封装
        """
        return self.fs_batch_delete({"fid[0]": id}, **request_kwargs)

    def fs_file(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取文件或文件夹的简略信息
        GET https://webapi.115.com/files/file
        payload:
            - file_id: int | str
        """
        api = "https://webapi.115.com/files/file"
        if isinstance(payload, (int, str)):
            payload = {"file_id": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_files(
        self, 
        payload: dict = {}, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取文件夹的中的文件列表和基本信息
        GET https://webapi.115.com/files
        payload:
            - cid: int | str = 0 # 文件夹 id
            - limit: int = 32    # 一页大小，意思就是 page_size
            - offset: int = 0    # 索引偏移，索引从 0 开始计算
            - asc: 0 | 1 = 1     # 是否升序排列
            - o: str = "file_name"
                # 用某字段排序：
                # - 文件名："file_name"
                # - 文件大小："file_size"
                # - 文件种类："file_type"
                # - 修改时间："user_utime"
                # - 创建时间："user_ptime"
                # - 上次打开时间："user_otime"

            - aid: int | str = 1
            - code: int | str = <default>
            - count_folders: 0 | 1 = 1
            - custom_order: int | str = <default>
            - fc_mix: 0 | 1 = <default> # 是否文件夹置顶，0 为置顶
            - format: str = "json"
            - is_q: 0 | 1 = <default>
            - is_share: 0 | 1 = <default>
            - natsort: 0 | 1 = <default>
            - record_open_time: 0 | 1 = 1
            - scid: int | str = <default>
            - show_dir: 0 | 1 = 1
            - snap: 0 | 1 = <default>
            - source: str = <default>
            - star: 0 | 1 = <default> # 是否星标文件
            - suffix: str = <default> # 后缀名
            - type: int | str = <default>
                # 文件类型：
                # - 所有: 0
                # - 文档: 1
                # - 图片: 2
                # - 音频: 3
                # - 视频: 4
                # - 压缩包: 5
                # - 应用: 6
                # - 书籍: 7
        """
        api = "https://webapi.115.com/files"
        payload = {"aid": 1, "asc": 1, "cid": 0, "count_folders": 1, "limit": 32, "o": "file_name", 
                   "offset": 0, "record_open_time": 1, "show_dir": 1, **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def fs_files2(
        self, 
        payload: dict = {}, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取文件夹的中的文件列表和基本信息
        GET https://aps.115.com/natsort/files.php
        payload:
            - cid: int | str = 0 # 文件夹 id
            - limit: int = 32    # 一页大小，意思就是 page_size
            - offset: int = 0    # 索引偏移，索引从 0 开始计算
            - asc: 0 | 1 = 1     # 是否升序排列
            - o: str = "file_name"
                # 用某字段排序：
                # - 文件名："file_name"
                # - 文件大小："file_size"
                # - 文件种类："file_type"
                # - 修改时间："user_utime"
                # - 创建时间："user_ptime"
                # - 上次打开时间："user_otime"

            - aid: int | str = 1
            - code: int | str = <default>
            - count_folders: 0 | 1 = 1
            - custom_order: int | str = <default>
            - fc_mix: 0 | 1 = <default> # 是否文件夹置顶，0 为置顶
            - format: str = "json"
            - is_q: 0 | 1 = <default>
            - is_share: 0 | 1 = <default>
            - natsort: 0 | 1 = <default>
            - record_open_time: 0 | 1 = 1
            - scid: int | str = <default>
            - show_dir: 0 | 1 = 1
            - snap: 0 | 1 = <default>
            - source: str = <default>
            - star: 0 | 1 = <default> # 是否星标文件
            - suffix: str = <default> # 后缀名
            - type: int | str = <default>
                # 文件类型：
                # - 所有: 0
                # - 文档: 1
                # - 图片: 2
                # - 音频: 3
                # - 视频: 4
                # - 压缩包: 5
                # - 应用: 6
                # - 书籍: 7
        """
        api = "https://aps.115.com/natsort/files.php"
        payload = {"aid": 1, "asc": 1, "cid": 0, "count_folders": 1, "limit": 32, "o": "file_name", 
                   "offset": 0, "record_open_time": 1, "show_dir": 1, **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def fs_files_type(
        self, 
        payload: Literal[1,2,3,4,5,6,7] | dict = 1, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取文件夹中某个文件类型的扩展名的（去重）列表
        GET https://webapi.115.com/files/get_second_type
        payload:
            - cid: int | str = 0 # 文件夹 id
            - type: int = <default>
                # 文件类型：
                # - 文档: 1
                # - 图片: 2
                # - 音频: 3
                # - 视频: 4
                # - 压缩包: 5
                # - 应用: 6
                # - 书籍: 7
            - file_label: int | str = <default>
        """
        api = "https://webapi.115.com/files/get_second_type"
        if isinstance(payload, int):
            payload = {"cid": 0, "type": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def fs_files_edit(
        self, 
        payload: list | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """设置文件或文件夹（备注、标签等）
        POST https://webapi.115.com/files/edit
        payload:
            # 如果是单个文件或文件夹
            - fid: int | str
            # 如果是多个文件或文件夹
            - fid[]: int | str
            - fid[]: int | str
            - ...
            # 其它配置信息
            - file_desc: str = <default> # 可以用 html
            - file_label: int | str = <default> # 标签 id，如果有多个，用逗号 "," 隔开
            - fid_cover: int | str = <default> # 封面图片的文件 id，如果有多个，用逗号 "," 隔开，如果要删除，值设为 0 即可
        """
        api = "https://webapi.115.com/files/edit"
        if (headers := request_kwargs.get("headers")):
            headers = request_kwargs["headers"] = dict(headers)
        else:
            headers = request_kwargs["headers"] = {}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        request_kwargs.pop("parse", None)
        return self.request(
            api, 
            "POST", 
            data=urlencode(payload), 
            **request_kwargs, 
        )

    def fs_files_batch_edit(
        self, 
        payload: list | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """批量设置文件或文件夹（显示时长等）
        payload:
            - show_play_long[{fid}]: 0 | 1 = 1 # 设置或取消显示时长
        """
        api = "https://webapi.115.com/files/batch_edit"
        if (headers := request_kwargs.get("headers")):
            headers = request_kwargs["headers"] = dict(headers)
        else:
            headers = request_kwargs["headers"] = {}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        request_kwargs.pop("parse", None)
        return self.request(
            api, 
            "POST", 
            data=urlencode(payload), 
            **request_kwargs, 
        )

    def fs_files_hidden(
        self, 
        payload: int | str | Iterable[int | str] | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """隐藏或者取消隐藏文件或文件夹
        POST https://webapi.115.com/files/hiddenfiles
        payload:
            - fid[0]: int | str
            - fid[1]: int | str
            - ...
            - hidden: 0 | 1 = 1
        """
        api = "https://webapi.115.com/files/hiddenfiles"
        if isinstance(payload, (int, str)):
            payload = {"hidden": 1, "fid[0]": payload}
        elif isinstance(payload, dict):
            payload = {"hidden": 1, **payload}
        else:
            payload = {f"f[{i}]": f for i, f in enumerate(payload)}
            if not payload:
                return {"state": False, "message": "no op"}
            payload["hidden"] = 1
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_hidden_switch(
        self, 
        payload: str | dict = "", 
        /, 
        **request_kwargs, 
    ) -> dict:
        """切换隐藏模式
        POST https://115.com/?ct=hiddenfiles&ac=switching
        payload:
            safe_pwd: str = "" # 密码，如果需要进入隐藏模式，请传递此参数
            show: 0 | 1 = <default>
            valid_type: int = 1
        """
        api = "https://115.com/?ct=hiddenfiles&ac=switching"
        if isinstance(payload, str):
            if payload:
                payload = {"valid_type": 1, "show": 1, "safe_pwd": payload}
            else:
                payload = {"valid_type": 1, "show": 0}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_statistic(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取文件或文件夹的统计信息（提示：但得不到根目录的统计信息，所以 cid 为 0 时无意义）
        GET https://webapi.115.com/category/get
        payload:
            cid: int | str
            aid: int | str = 1
        """
        api = "https://webapi.115.com/category/get"
        if isinstance(payload, (int, str)):
            payload = {"cid": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def fs_get_repeat(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """查找重复文件（罗列除此以外的 sha1 相同的文件）
        GET https://webapi.115.com/files/get_repeat_sha
        payload:
            file_id: int | str
            offset: int = 0
            limit: int = 1150
            source: str = ""
            format: str = "json"
        """
        api = "https://webapi.115.com/files/get_repeat_sha"
        if isinstance(payload, (int, str)):
            payload = {"offset": 0, "limit": 1150, "format": "json", "file_id": payload}
        else:
            payload = {"offset": 0, "limit": 1150, "format": "json", **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def fs_index_info(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取当前已用空间、可用空间、登录设备等信息
        GET https://webapi.115.com/files/index_info
        """
        api = "https://webapi.115.com/files/index_info"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def fs_info(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取文件或文件夹的基本信息
        GET https://webapi.115.com/files/get_info
        payload:
            - file_id: int | str
        """
        api = "https://webapi.115.com/files/get_info"
        if isinstance(payload, (int, str)):
            payload = {"file_id": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def fs_mkdir(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """新建文件夹
        POST https://webapi.115.com/files/add
        payload:
            - cname: str
            - pid: int | str = 0
        """
        api = "https://webapi.115.com/files/add"
        if isinstance(payload, str):
            payload = {"pid": 0, "cname": payload}
        else:
            payload = {"pid": 0, **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_move(
        self, 
        id: int | str, 
        /, 
        pid: int = 0, 
        **request_kwargs, 
    ) -> dict:
        """移动文件或文件夹，此接口是对 `fs_batch_move` 的封装
        """
        return self.fs_batch_move({"fid[0]": id, "pid": pid}, **request_kwargs)

    def fs_rename(
        self, 
        id: int, 
        name: str, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """重命名文件或文件夹，此接口是对 `fs_batch_rename` 的封装
        """
        return self.fs_batch_rename({f"files_new_name[{id}]": name}, **request_kwargs)

    def fs_search(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """搜索文件或文件夹（提示：好像最多只能罗列前 10,000 条数据，也就是 limit + offset <= 10_000）
        GET https://webapi.115.com/files/search
        payload:
            - aid: int | str = 1
            - asc: 0 | 1 = <default> # 是否升序排列
            - cid: int | str = 0 # 文件夹 id
            - count_folders: 0 | 1 = <default>
            - date: str = <default> # 筛选日期
            - fc_mix: 0 | 1 = <default> # 是否文件夹置顶，0 为置顶
            - file_label: int | str = <default> # 标签 id
            - format: str = "json" # 输出格式（不用管）
            - limit: int = 32 # 一页大小，意思就是 page_size
            - o: str = <default>
                # 用某字段排序：
                # - 文件名："file_name"
                # - 文件大小："file_size"
                # - 文件种类："file_type"
                # - 修改时间："user_utime"
                # - 创建时间："user_ptime"
                # - 上次打开时间："user_otime"
            - offset: int = 0  # 索引偏移，索引从 0 开始计算
            - pick_code: str = <default>
            - search_value: str = <default>
            - show_dir: 0 | 1 = 1
            - source: str = <default>
            - star: 0 | 1 = <default>
            - suffix: str = <default>
            - type: int | str = <default>
                # 文件类型：
                # - 所有: 0
                # - 文档: 1
                # - 图片: 2
                # - 音频: 3
                # - 视频: 4
                # - 压缩包: 5
                # - 应用: 6
        """
        api = "https://webapi.115.com/files/search"
        if isinstance(payload, str):
            payload = {"aid": 1, "cid": 0, "format": "json", "limit": 32, "offset": 0, "show_dir": 1, "search_value": payload}
        else:
            payload = {"aid": 1, "cid": 0, "format": "json", "limit": 32, "offset": 0, "show_dir": 1, **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def fs_export_dir(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """导出目录树
        POST https://webapi.115.com/files/export_dir
        payload:
            file_ids: int | str   # 有多个时，用逗号 "," 隔开
            target: str = "U_1_0" # 导出目录树到这个目录
            layer_limit: int = <default> # 层级深度，自然数
        """
        api = "https://webapi.115.com/files/export_dir"
        if isinstance(payload, (int, str)):
            payload = {"file_ids": payload, "target": "U_1_0"}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_export_dir_status(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取导出目录树的完成情况
        GET https://webapi.115.com/files/export_dir
        payload:
            export_id: int | str
        """
        api = "https://webapi.115.com/files/export_dir"
        if isinstance(payload, (int, str)):
            payload = {"export_id": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def fs_export_dir_future(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> ExportDirStatus:
        """执行导出目录树，新开启一个线程，用于检查完成状态
        payload:
            file_ids: int | str   # 有多个时，用逗号 "," 隔开
            target: str = "U_1_0" # 导出目录树到这个目录
            layer_limit: int = <default> # 层级深度，自然数
        """
        resp = check_response(self.fs_export_dir(payload, **request_kwargs))
        return ExportDirStatus(self, resp["data"]["export_id"])

    def fs_shortcut_get(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """罗列所有的快捷入口
        GET https://webapi.115.com/category/shortcut
        """
        api = "https://webapi.115.com/category/shortcut"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def fs_shortcut_set(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """把一个目录设置或取消为快捷入口
        POST https://webapi.115.com/category/shortcut
        payload:
            file_id: int | str # 有多个时，用逗号 "," 隔开
            op: "add" | "delete" = "add"
        """
        api = "https://webapi.115.com/category/shortcut"
        if isinstance(payload, (int, str)):
            payload = {"file_id": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_cover(
        self, 
        fids: int | str | Iterable[int | str], 
        /, 
        fid_cover: int | str = 0, 
        **request_kwargs, 
    ) -> dict:
        """设置目录的封面，此接口是对 `fs_files_edit` 的封装

        :param fids: 单个或多个文件或文件夹 id
        :param file_label: 图片的 id，如果为 0 则是删除封面
        """
        api = "https://webapi.115.com/label/delete"
        if isinstance(fids, (int, str)):
            payload = [("fid", fids)]
        else:
            payload = [("fid[]", fid) for fid in fids]
            if not payload:
                return {"state": False, "message": "no op"}
        payload.append(("fid_cover", fid_cover))
        request_kwargs.pop("parse", None)
        return self.fs_files_edit(payload, **request_kwargs)

    def fs_desc_get(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取文件或文件夹的备注
        GET https://webapi.115.com/files/desc
        payload:
            - file_id: int | str
            - format: str = "json"
            - compat: 0 | 1 = 1
            - new_html: 0 | 1 = 1
        """
        api = "https://webapi.115.com/files/desc"
        if isinstance(payload, (int, str)):
            payload = {"format": "json", "compat": 1, "new_html": 1, "file_id": payload}
        else:
            payload = {"format": "json", "compat": 1, "new_html": 1, **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def fs_desc(
        self, 
        fids: int | str | Iterable[int | str], 
        /, 
        file_desc: str = "", 
        **request_kwargs, 
    ) -> dict:
        """为文件或文件夹设置备注，最多允许 65535 个字节 (64 KB 以内)，此接口是对 `fs_files_edit` 的封装

        :param fids: 单个或多个文件或文件夹 id
        :param file_desc: 备注信息，可以用 html
        """
        if isinstance(fids, (int, str)):
            payload = [("fid", fids)]
        else:
            payload = [("fid[]", fid) for fid in fids]
            if not payload:
                return {"state": False, "message": "no op"}
        payload.append(("file_desc", file_desc))
        request_kwargs.pop("parse", None)
        return self.fs_files_edit(payload, **request_kwargs)

    def fs_label(
        self, 
        fids: int | str | Iterable[int | str], 
        /, 
        file_label: int | str = "", 
        **request_kwargs, 
    ) -> dict:
        """为文件或文件夹设置标签，此接口是对 `fs_files_edit` 的封装

        :param fids: 单个或多个文件或文件夹 id
        :param file_label: 标签 id，如果有多个，用逗号 "," 隔开
        """
        if isinstance(fids, (int, str)):
            payload = [("fid", fids)]
        else:
            payload = [("fid[]", fid) for fid in fids]
            if not payload:
                return {"state": False, "message": "no op"}
        payload.append(("file_label", file_label))
        request_kwargs.pop("parse", None)
        return self.fs_files_edit(payload, **request_kwargs)

    def fs_label_batch(
        self, 
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """批量设置标签
        POST https://webapi.115.com/files/batch_label
        payload:
            - action: "add" | "remove" | "reset" | "replace"
                # 操作名
                # - 添加: "add"
                # - 移除: "remove"
                # - 重设: "reset"
                # - 替换: "replace"
            - file_ids: int | str # 文件或文件夹 id，如果有多个，用逗号 "," 隔开
            - file_label: int | str = <default> # 标签 id，如果有多个，用逗号 "," 隔开
            - file_label[{file_label}]: int | str = <default> # action 为 replace 时使用此参数，file_label[{原标签id}]: {目标标签id}，例如 file_label[123]: 456，就是把 id 是 123 的标签替换为 id 是 456 的标签
        """
        api = "https://webapi.115.com/files/batch_label"
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_score(
        self, 
        file_id: int | str, 
        /, 
        score: int = 0, 
        **request_kwargs, 
    ) -> dict:
        """给文件或文件夹评分
        POST https://webapi.115.com/files/score
        payload:
            - file_id: int | str # 文件或文件夹 id，如果有多个，用逗号 "," 隔开
            - score: int = 0     # 0 为删除评分
        """
        api = "https://webapi.115.com/files/score"
        payload = {"file_id": file_id, "score": score}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def fs_star(
        self, 
        file_id: int | str, 
        /, 
        star: bool = True, 
        **request_kwargs, 
    ) -> dict:
        """为文件或文件夹设置或取消星标
        POST https://webapi.115.com/files/star
        payload:
            - file_id: int | str # 文件或文件夹 id，如果有多个，用逗号 "," 隔开
            - star: 0 | 1 = 1
        """
        api = "https://webapi.115.com/files/star"
        payload = {"file_id": file_id, "star": int(star)}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def label_add(
        self, 
        /, 
        *lables: str, 
        **request_kwargs, 
    ) -> dict:
        """添加标签（可以接受多个）
        POST https://webapi.115.com/label/add_multi

        可传入多个 label 描述，每个 label 的格式都是 "{label_name}\x07{color}"，例如 "tag\x07#FF0000"
        """
        api = "https://webapi.115.com/label/add_multi"
        payload = [("name[]", label) for label in lables if label]
        if not payload:
            return {"state": False, "message": "no op"}
        if (headers := request_kwargs.get("headers")):
            headers = request_kwargs["headers"] = dict(headers)
        else:
            headers = request_kwargs["headers"] = {}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        request_kwargs.pop("parse", None)
        return self.request(
            api, 
            "POST", 
            data=urlencode(payload), 
            **request_kwargs, 
        )

    def label_del(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """删除标签
        POST https://webapi.115.com/label/delete
        payload:
            - id: int | str # 标签 id，如果有多个，用逗号 "," 隔开
        """
        api = "https://webapi.115.com/label/delete"
        if isinstance(payload, (int, str)):
            payload = {"id": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def label_edit(
        self, 
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """编辑标签
        POST https://webapi.115.com/label/edit
        payload:
            - id: int | str # 标签 id
            - name: str = <default>  # 标签名
            - color: str = <default> # 标签颜色，支持 css 颜色语法
            - sort: int = <default> # 序号
        """
        api = "https://webapi.115.com/label/edit"
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def label_list(
        self, 
        payload: dict = {}, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """罗列标签列表（如果要获取做了标签的文件列表，用 `fs_search` 接口）
        GET https://webapi.115.com/label/list
        payload:
            - offset: int = 0 # 索引偏移，从 0 开始
            - limit: int = 11500 # 一页大小
            - keyword: str = <default> # 搜索关键词
            - sort: "name" | "update_time" | "create_time" = <default>
                # 排序字段:
                # - 名称: "name"
                # - 创建时间: "create_time"
                # - 更新时间: "update_time"
            - order: "asc" | "desc" = <default> # 排序顺序："asc"(升序), "desc"(降序)
        """
        api = "https://webapi.115.com/label/list"
        payload = {"offset": 0, "limit": 11500, **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def life_list(
        self, 
        payload: dict = {}, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """罗列登录和增删改操作记录（最新几条）
        GET https://life.115.com/api/1.0/web/1.0/life/life_list
        payload:
            - start: int = 0
            - limit: int = 1000
            - show_type: int | str = 0
                # 筛选类型，有多个则用逗号 ',' 隔开:
                # 0: all
                # 1: upload_file
                # 2: browse_document
                # 3: <UNKNOWN>
                # 4: account_security
            - type: int | str = <default>
            - tab_type: int | str = <default>
            - file_behavior_type: int | str = <default>
            - mode: str = <default>
            - check_num: int = <default>
            - total_count: int = <default>
            - start_time: int = <default>
            - end_time: int = <default> # 默认为次日零点前一秒
            - show_note_cal: 0 | 1 = <default>
            - isShow: 0 | 1 = <default>
            - isPullData: 'true' | 'false' = <default>
            - last_data: str = <default> # JSON object, e.g. {"last_time":1700000000,"last_count":1,"total_count":200}
        """
        api = "https://life.115.com/api/1.0/web/1.0/life/life_list"
        now = datetime.now()
        datetime.combine(now.date(), now.time().max)
        payload = {
            "start": 0, 
            "limit": 1000, 
            "show_type": 0, 
            "end_time": int(datetime.combine(now.date(), now.time().max).timestamp()), 
            **payload, 
        }
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def behavior_detail(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取增删改操作记录明细
        payload:
            - type: str
                # 操作类型
                # - "new_folder":    新增文件夹
                # - "copy_folder":   复制文件夹
                # - "folder_rename": 文件夹改名
                # - "move_file":     移动文件或文件夹
                # - "delete_file":   删除文件或文件夹
                # - "upload_file":   上传文件
                # - "rename_file":   文件改名（未实现）
                # - "copy_file":     复制文件（未实现）
            - limit: int = 32
            - offset: int = 0
            - date: str = <default> # 默认为今天，格式为 yyyy-mm-dd
        """
        api = "https://proapi.115.com/android/1.0/behavior/detail"
        if isinstance(payload, str):
            payload = {"limit": 32, "offset": 0, "date": str(date.today()), "type": payload}
        else:
            payload = {"limit": 32, "offset": 0, "date": str(date.today()), **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    ########## Share API ##########

    def share_send(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """创建（自己的）分享
        POST https://webapi.115.com/share/send
        payload:
            - file_ids: int | str # 文件列表，有多个用逗号 "," 隔开
            - is_asc: 0 | 1 = 1 # 是否升序排列
            - order: str = "file_name"
                # 用某字段排序：
                # - 文件名："file_name"
                # - 文件大小："file_size"
                # - 文件种类："file_type"
                # - 修改时间："user_utime"
                # - 创建时间："user_ptime"
                # - 上次打开时间："user_otime"
            - ignore_warn: 0 | 1 = 1 # 忽略信息提示，传 1 就行了
            - user_id: int | str = <default>
        """
        api = "https://webapi.115.com/share/send"
        if isinstance(payload, (int, str)):
            payload = {"ignore_warn": 1, "is_asc": 1, "order": "file_name", "file_ids": payload}
        else:
            payload = {"ignore_warn": 1, "is_asc": 1, "order": "file_name", **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def share_info(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取（自己的）分享信息
        GET https://webapi.115.com/share/shareinfo
        payload:
            - share_code: str
        """
        api = "https://webapi.115.com/share/shareinfo"
        if isinstance(payload, str):
            payload = {"share_code": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def share_list(
        self, 
        payload: dict = {}, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """罗列（自己的）分享信息列表
        GET https://webapi.115.com/share/slist
        payload:
            - limit: int = 32
            - offset: int = 0
            - user_id: int | str = <default>
        """
        api = "https://webapi.115.com/share/slist"
        payload = {"offset": 0, "limit": 32, **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def share_update(
        self, 
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """变更（自己的）分享的配置（例如改访问密码，取消分享）
        POST https://webapi.115.com/share/updateshare
        payload:
            - share_code: str
            - receive_code: str = <default>         # 访问密码（口令）
            - share_duration: int = <default>       # 分享天数: 1(1天), 7(7天), -1(长期)
            - is_custom_code: 0 | 1 = <default>     # 用户自定义口令（不用管）
            - auto_fill_recvcode: 0 | 1 = <default> # 分享链接自动填充口令（不用管）
            - share_channel: int = <default>        # 分享渠道代码（不用管）
            - action: str = <default>               # 操作: 取消分享 "cancel"
        """
        api = "https://webapi.115.com/share/updateshare"
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    @staticmethod
    def share_snap(
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取分享链接的某个文件夹中的文件和子文件夹的列表（包含详细信息）
        GET https://webapi.115.com/share/snap
        payload:
            - share_code: str
            - receive_code: str
            - cid: int | str = 0
            - limit: int = 32
            - offset: int = 0
            - asc: 0 | 1 = <default> # 是否升序排列
            - o: str = <default>
                # 用某字段排序：
                # - 文件名："file_name"
                # - 文件大小："file_size"
                # - 文件种类："file_type"
                # - 修改时间："user_utime"
                # - 创建时间："user_ptime"
                # - 上次打开时间："user_otime"
        """
        api = "https://webapi.115.com/share/snap"
        payload = {"cid": 0, "limit": 32, "offset": 0, **payload}
        request_kwargs.pop("parse", None)
        return request(api, params=payload, **request_kwargs)

    def share_downlist(
        self, 
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取分享链接的某个文件夹中可下载的文件的列表（只含文件，不含文件夹，任意深度，简略信息）
        GET https://proapi.115.com/app/share/downlist
        payload:
            - share_code: str
            - receive_code: str
            - cid: int | str = 0
        """
        api = "https://proapi.115.com/app/share/downlist"
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def share_receive(
        self, 
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """接收分享链接的某些文件或文件夹
        POST https://webapi.115.com/share/receive
        payload:
            - share_code: str
            - receive_code: str
            - file_id: int | str             # 有多个时，用逗号 "," 分隔
            - cid: int | str = 0             # 这是你网盘的文件夹 cid
            - user_id: int | str = <default>
        """
        api = "https://webapi.115.com/share/receive"
        payload = {"cid": 0, **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def share_download_url_web(
        self, 
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取分享链接中某个文件的下载链接（网页版接口，不推荐使用）
        GET https://webapi.115.com/share/downurl
        payload:
            - file_id: int | str
            - receive_code: str
            - share_code: str
            - user_id: int | str = <default>
        """
        api = "https://webapi.115.com/share/downurl"
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def share_download_url_app(
        self, 
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取分享链接中某个文件的下载链接
        POST https://proapi.115.com/app/share/downurl
        payload:
            - file_id: int | str
            - receive_code: str
            - share_code: str
            - user_id: int | str = <default>
        """
        api = "https://proapi.115.com/app/share/downurl"
        def parse(resp, content: bytes) -> dict:
            resp = loads(content)
            if resp["state"]:
                resp["data"] = loads(RSA_ENCODER.decode(resp["data"]))
            return resp
        request_kwargs["parse"] = parse
        data = RSA_ENCODER.encode(dumps(payload))
        return self.request(api, "POST", data={"data": data}, **request_kwargs)

    def share_download_url(
        self, 
        payload: dict, 
        /, 
        detail: bool = False, 
        strict: bool = True, 
        **request_kwargs, 
    ) -> str:
        """获取分享链接中某个文件的下载链接，此接口是对 `share_download_url_app` 的封装
        POST https://proapi.115.com/app/share/downurl
        payload:
            - file_id: int | str
            - receive_code: str
            - share_code: str
            - user_id: int | str = <default>
        """
        file_id = payload["file_id"]
        async_ = request_kwargs.get("async_", False)
        resp = self.share_download_url_app(payload, **request_kwargs)
        def get_url(resp: dict) -> str:
            info = check_response(resp)["data"]
            if not info:
                raise FileNotFoundError(errno.ENOENT, f"no such id: {file_id!r}")
            url = info["url"]
            if strict and not url:
                raise IsADirectoryError(errno.EISDIR, f"{file_id} is a directory")
            if not detail:
                return url["url"] if url else ""
            return UrlStr(
                url["url"] if url else "", 
                id=int(info["fid"]), 
                file_name=info["fn"], 
                file_size=int(info["fs"]), 
                is_directory=not url, 
            )
        if async_:
            async def async_request() -> str:
                return get_url(await resp)
            return async_request()
        else:
            return get_url(resp)

    ########## Download API ##########

    def download_url_web(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取文件的下载链接（网页版接口，不推荐使用）
        GET https://webapi.115.com/files/download
        payload:
            - pickcode: str
        """
        api = "https://webapi.115.com/files/download"
        if isinstance(payload, str):
            payload = {"pickcode": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def download_url_app(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取文件的下载链接
        POST https://proapi.115.com/app/chrome/downurl
        payload:
            - pickcode: str
        """
        api = "https://proapi.115.com/app/chrome/downurl"
        if isinstance(payload, str):
            payload = {"pickcode": payload}
        def parse(resp, content: bytes) -> dict:
            resp = loads(content)
            if resp["state"]:
                resp["data"] = loads(RSA_ENCODER.decode(resp["data"]))
            return resp
        request_kwargs["parse"] = parse
        return self.request(
            api, 
            "POST", 
            data={"data": RSA_ENCODER.encode(dumps(payload))}, 
            **request_kwargs, 
        )

    def download_url(
        self, 
        pickcode: str, 
        /, 
        detail: bool = False, 
        strict: bool = True, 
        use_web_api: bool = False, 
        **request_kwargs, 
    ) -> str:
        """获取文件的下载链接，此接口是对 `download_url_app` 的封装
        """
        async_ = request_kwargs.get("async_", False)
        if use_web_api:
            resp = self.download_url_web(
                {"pickcode": pickcode}, 
                **request_kwargs, 
            )
            def get_url(resp: dict) -> str:
                if "pickcode" not in resp:
                    raise FileNotFoundError(errno.ENOENT, f"no such pickcode: {pickcode!r}")
                if detail:
                    return UrlStr(
                        resp.get("file_url", ""), 
                        id=int(resp["file_id"]), 
                        pickcode=resp["pickcode"], 
                        file_name=resp["file_name"], 
                        file_size=int(resp["file_size"]), 
                        is_directory=not resp["state"], 
                    )
                return resp.get("file_url", "")
        else:
            resp = self.download_url_app(
                {"pickcode": pickcode}, 
                **request_kwargs, 
            )
            def get_url(resp: dict) -> str:
                if not resp["state"]:
                    raise FileNotFoundError(errno.ENOENT, f"no such pickcode: {pickcode!r}")
                for fid, info in resp["data"].items():
                    url = info["url"]
                    if strict and not url:
                        raise IsADirectoryError(errno.EISDIR, f"{fid} is a directory")
                    if not detail:
                        return url["url"] if url else ""
                    return UrlStr(
                        url["url"] if url else "", 
                        id=int(fid), 
                        pickcode=info["pick_code"], 
                        file_name=info["file_name"], 
                        file_size=int(info["file_size"]), 
                        is_directory=not url,
                    )
                raise FileNotFoundError(errno.ENOENT, f"no such pickcode: {pickcode!r}")
        if async_:
            async def async_request() -> str:
                return get_url(await resp) 
            return async_request()
        else:
            return get_url(resp)

    ########## Upload API ##########

    @staticmethod
    def _oss_upload_sign(
        bucket_name: str, 
        key: str, 
        token: dict, 
        method: str = "PUT", 
        params: None | str | Mapping | Sequence[tuple[Any, Any]] = "", 
        headers: None | str | dict = "", 
    ) -> dict:
        """帮助函数：计算认证信息，返回带认证信息的请求头
        """
        subresource_key_set = frozenset((
            "response-content-type", "response-content-language",
            "response-cache-control", "logging", "response-content-encoding",
            "acl", "uploadId", "uploads", "partNumber", "group", "link",
            "delete", "website", "location", "objectInfo", "objectMeta",
            "response-expires", "response-content-disposition", "cors", "lifecycle",
            "restore", "qos", "referer", "stat", "bucketInfo", "append", "position", "security-token",
            "live", "comp", "status", "vod", "startTime", "endTime", "x-oss-process",
            "symlink", "callback", "callback-var", "tagging", "encryption", "versions",
            "versioning", "versionId", "policy", "requestPayment", "x-oss-traffic-limit", "qosInfo", "asyncFetch",
            "x-oss-request-payer", "sequential", "inventory", "inventoryId", "continuation-token", "callback",
            "callback-var", "worm", "wormId", "wormExtend", "replication", "replicationLocation",
            "replicationProgress", "transferAcceleration", "cname", "metaQuery",
            "x-oss-ac-source-ip", "x-oss-ac-subnet-mask", "x-oss-ac-vpc-id", "x-oss-ac-forward-allow",
            "resourceGroup", "style", "styleName", "x-oss-async-process", "regionList"
        ))
        date = formatdate(usegmt=True)
        if params is None:
            params = ""
        else:
            if not isinstance(params, str):
                if isinstance(params, dict):
                    if params.keys() - subresource_key_set:
                        params = [(k, params[k]) for k in params.keys() & subresource_key_set]
                elif isinstance(params, Mapping):
                    params = [(k, params[k]) for k in params if k in subresource_key_set]
                else:
                    params = [(k, v) for k, v in params if k in subresource_key_set]
                params = urlencode(params)
            if params:
                params = "?" + params
        if headers is None:
            headers = ""
        elif isinstance(headers, dict):
            it = (
                (k2, v)
                for k, v in headers.items()
                if (k2 := k.lower()).startswith("x-oss-")
            )
            headers = "\n".join("%s:%s" % e for e in sorted(it))
        signature_data = f"""{method.upper()}


{date}
{headers}
/{bucket_name}/{key}{params}""".encode("utf-8")
        signature = to_base64(hmac_digest(bytes(token["AccessKeySecret"], "utf-8"), signature_data, "sha1"))
        return {
            "date": date, 
            "authorization": "OSS {0}:{1}".format(token["AccessKeyId"], signature), 
        }

    def _oss_upload_request(
        self, 
        /, 
        bucket_name: str, 
        key: str, 
        url: str, 
        token: dict, 
        method: str = "PUT", 
        params: None | str | dict | list[tuple] = None, 
        headers: None | dict = None, 
        **request_kwargs, 
    ):
        """帮助函数：请求阿里云 OSS （115 目前所使用的阿里云的对象存储）的公用函数
        """
        headers2 = self._oss_upload_sign(
            bucket_name, 
            key, 
            token, 
            method=method, 
            params=params, 
            headers=headers, 
        )
        if headers:
            headers2.update(headers)
        headers2["Content-Type"] = ""
        return self.request(
            url, 
            params=params, 
            headers=headers2, 
            method=method, 
            **request_kwargs, 
        )

    # NOTE: https://github.com/aliyun/aliyun-oss-python-sdk/blob/master/oss2/api.py#L1359-L1595
    def _oss_multipart_upload_init(
        self, 
        /, 
        bucket_name: str, 
        key: str, 
        url: str, 
        token: dict, 
        **request_kwargs, 
    ) -> str:
        """帮助函数：分片上传的初始化，获取 upload_id
        """
        request_kwargs["parse"] = lambda resp, content, /: getattr(fromstring(content).find("UploadId"), "text")
        request_kwargs["method"] = "POST"
        request_kwargs["params"] = "uploads"
        request_kwargs["headers"] = {"x-oss-security-token": token["SecurityToken"]}
        return self._oss_upload_request(
            bucket_name, 
            key, 
            url, 
            token, 
            **request_kwargs, 
        )

    def _oss_multipart_upload_part(
        self, 
        /, 
        file: ( bytes | bytearray | memoryview | 
                SupportsRead[bytes] | SupportsRead[bytearray] | SupportsRead[memoryview] | 
                Iterable[bytes] | Iterable[bytearray] | Iterable[memoryview] | 
                AsyncIterable[bytes] | AsyncIterable[bytearray] | AsyncIterable[memoryview] ), 
        bucket_name: str, 
        key: str, 
        url: str, 
        token: dict, 
        upload_id: str, 
        part_number: int, 
        part_size: int = 10 * 1 << 20, # default to: 10 MB
        **request_kwargs, 
    ) -> dict:
        """帮助函数：上传一个分片，返回一个字典，包含如下字段：

            {
                "PartNumber": int,    # 分块序号，从 1 开始计数
                "LastModified": str,  # 最近更新时间
                "ETag": str,          # ETag 值，判断资源是否发生变化
                "HashCrc64ecma": int, # 校验码
                "Size": int,          # 分片大小
            }
        """
        async_ = request_kwargs.get("async_", False)
        def parse(resp, /) -> dict:
            headers = resp.headers
            return {
                "PartNumber": part_number, 
                "LastModified": datetime.strptime(headers["date"], "%a, %d %b %Y %H:%M:%S GMT").strftime("%FT%X.%f")[:-3] + "Z", 
                "ETag": headers["ETag"], 
                "HashCrc64ecma": int(headers["x-oss-hash-crc64ecma"]), 
                "Size": count_in_bytes, 
            }
        request_kwargs["parse"] = parse
        request_kwargs["params"] = {"partNumber": part_number, "uploadId": upload_id}
        request_kwargs["headers"] = {"x-oss-security-token": token["SecurityToken"]}
        if isinstance(file, (bytes, bytearray, memoryview)):
            count_in_bytes = len(file)
            if async_:
                async def make_iter(file):
                    yield file
                file = make_iter(file)
            else:
                file = iter((file,))
        elif hasattr(file, "read"):
            count_in_bytes = 0
            def acc(length):
                nonlocal count_in_bytes
                count_in_bytes += length
            if not async_ and iscoroutinefunction(file.read):
                async_ = request_kwargs["async_"] = True
            if async_:
                file = bio_chunk_async_iter(file, part_size, callback=acc)
            else:
                file = bio_chunk_iter(file, part_size, callback=acc)
        else:
            def acc(chunk):
                nonlocal count_in_bytes
                count_in_bytes += len(chunk)
                if count_in_bytes >= part_size:
                    raise StopIteration
            if async_ or isinstance(file, AsyncIterable):
                if async_:
                    file = ensure_aiter(file)
                else:
                    async_ = request_kwargs["async_"] = True
                file = wrap_aiter(file, callnext=acc)
            else:
                file = wrap_iter(file, callnext=acc)
        request_kwargs["data"] = file
        return self._oss_upload_request(
            bucket_name, 
            key, 
            url, 
            token, 
            **request_kwargs, 
        )

    def _oss_multipart_upload_complete(
        self, 
        /, 
        bucket_name: str, 
        key: str, 
        callback: dict, 
        url: str, 
        token: dict, 
        upload_id: str, 
        parts: list[dict], 
        **request_kwargs, 
    ) -> dict:
        """帮助函数：完成分片上传，会执行回调然后 115 上就能看到文件
        """
        request_kwargs["method"] = "POST"
        request_kwargs["params"] = {"uploadId": upload_id}
        request_kwargs["headers"] = {
            "x-oss-security-token": token["SecurityToken"], 
            "x-oss-callback": to_base64(callback["callback"]), 
            "x-oss-callback-var": to_base64(callback["callback_var"]), 
        }
        request_kwargs["data"] = ("<CompleteMultipartUpload>%s</CompleteMultipartUpload>" % "".join(map(
            "<Part><PartNumber>{PartNumber}</PartNumber><ETag>{ETag}</ETag></Part>".format_map, 
            parts, 
        ))).encode("utf-8")
        request_kwargs.pop("parse", None)
        return self._oss_upload_request(
            bucket_name, 
            key, 
            url, 
            token, 
            **request_kwargs, 
        )

    def _oss_multipart_upload_cancel(
        self, 
        /, 
        bucket_name: str, 
        key: str, 
        url: str, 
        token: dict, 
        upload_id: str, 
        **request_kwargs, 
    ) -> bool:
        """帮助函数：取消分片上传
        """
        request_kwargs["parse"] = lambda resp: 200 <= resp.status_code < 300 or resp.status_code == 404
        request_kwargs["method"] = "DELETE"
        request_kwargs["params"] = {"uploadId": upload_id}
        request_kwargs["headers"] = {"x-oss-security-token": token["SecurityToken"]}
        return self._oss_upload_request(
            bucket_name, 
            key, 
            url, 
            token, 
            **request_kwargs, 
        )

    def _oss_multipart_upload_part_iter(
        self, 
        /, 
        file: ( bytes | bytearray | memoryview | 
                SupportsRead[bytes] | SupportsRead[bytearray] | SupportsRead[memoryview] | 
                Iterable[bytes] | Iterable[bytearray] | Iterable[memoryview] | 
                AsyncIterable[bytes] | AsyncIterable[bytearray] | AsyncIterable[memoryview] ), 
        bucket_name: str, 
        key: str, 
        url: str, 
        token: dict, 
        upload_id: str, 
        part_number_start: int = 1, 
        part_size: int = 10 * 1 << 20, # default to: 10 MB
        **request_kwargs, 
    ) -> Iterator[dict]:
        """帮助函数：迭代器，迭代一次上传一个分片
        """
        async_ = request_kwargs.get("async_", False)
        if isinstance(file, (bytes, bytearray, memoryview)):
            if async_:
                file = bytes_to_chunk_async_iter(file, part_size)
            else:
                file = bytes_to_chunk_iter(file, part_size)
        elif not hasattr(file, "read"):
            if not async_ and iscoroutinefunction(file.read):
                async_ = request_kwargs["async_"] = True
            if async_:
                file = bytes_ensure_part_async_iter(file, part_size)
            else:
                file = bytes_ensure_part_iter(file, part_size) 
        if async_:
            async def async_request():
                for part_number in count(part_number_start):
                    part = await self._oss_multipart_upload_part(
                        file, 
                        bucket_name, 
                        key, 
                        url, 
                        token, 
                        upload_id, 
                        part_number=part_number, 
                        part_size=part_size, 
                        **request_kwargs, 
                    )
                    yield part
                    if part["Size"] < part_size:
                        break
            return async_request()
        else:
            def request():
                for part_number in count(part_number_start):
                    part = self._oss_multipart_upload_part(
                        file, 
                        bucket_name, 
                        key, 
                        url, 
                        token, 
                        upload_id, 
                        part_number=part_number, 
                        part_size=part_size, 
                        **request_kwargs, 
                    )
                    yield part
                    if part["Size"] < part_size:
                        break
            return request()

    def _oss_multipart_part_iter(
        self, 
        /, 
        bucket_name: str, 
        key: str, 
        url: str, 
        token: dict, 
        upload_id: str, 
        **request_kwargs, 
    ) -> Iterator[dict]:
        """帮助函数：上传文件到阿里云 OSS，罗列已经上传的分块
        """
        to_num = lambda s: int(s) if isinstance(s, str) and s.isnumeric() else s
        request_kwargs["method"] = "GET"
        request_kwargs["headers"] = {"x-oss-security-token": token["SecurityToken"]}
        request_kwargs["params"] = params = {"uploadId": upload_id}
        request_kwargs["parse"] = lambda resp, content, /: fromstring(content)
        async_ = request_kwargs.get("async_", False)
        if async_:
            async def async_request():
                while True:
                    etree = await self._oss_upload_request(
                        bucket_name, 
                        key, 
                        url, 
                        token, 
                        **request_kwargs, 
                    )
                    for el in etree.iterfind("Part"):
                        yield {sel.tag: to_num(sel.text) for sel in el}
                    if etree.find("IsTruncated").text == "false":
                        break
                    params["part-number-marker"] = etree.find("NextPartNumberMarker").text
            return async_request()
        else:
            def request():
                while True:
                    etree = self._oss_upload_request(
                        bucket_name, 
                        key, 
                        url, 
                        token, 
                        **request_kwargs, 
                    )
                    for el in etree.iterfind("Part"):
                        yield {sel.tag: to_num(sel.text) for sel in el}
                    if etree.find("IsTruncated").text == "false":
                        break
                    params["part-number-marker"] = etree.find("NextPartNumberMarker").text
            return request()

    def _oss_upload(
        self, 
        /, 
        file: ( bytes | bytearray | memoryview | 
                SupportsRead[bytes] | SupportsRead[bytearray] | SupportsRead[memoryview] | 
                Iterable[bytes] | Iterable[bytearray] | Iterable[memoryview] | 
                AsyncIterable[bytes] | AsyncIterable[bytearray] | AsyncIterable[memoryview] ), 
        bucket_name: str, 
        key: str, 
        callback: dict, 
        token: Optional[dict] = None, 
        **request_kwargs, 
    ) -> dict:
        """帮助函数：上传文件到阿里云 OSS，一次上传全部（即不进行分片）
        """
        url = self.upload_endpoint_url(bucket_name, key)
        async_ = request_kwargs.get("async_", False)
        if isinstance(file, (bytes, bytearray, memoryview)):
            if async_:
                async def make_iter(file):
                    yield file
                file = make_iter(file)
            else:
                file = iter((file,))
        elif hasattr(file, "read"):
            if not async_ and iscoroutinefunction(file.read):
                async_ = request_kwargs["async_"] = True
            if async_:
                file = bio_chunk_async_iter(file)
            else:
                file = bio_chunk_iter(file)
        elif isinstance(file, AsyncIterable):
            if not async_:
                async_ = request_kwargs["async_"] = True
        elif async_:
            file = ensure_aiter(file)
        request_kwargs["data"] = file
        if async_:
            async def async_request():
                nonlocal token
                if not token:
                    token = await self.upload_token(async_=True)
                request_kwargs["headers"] = {
                    "x-oss-security-token": token["SecurityToken"], 
                    "x-oss-callback": to_base64(callback["callback"]), 
                    "x-oss-callback-var": to_base64(callback["callback_var"]), 
                }
                return await self._oss_upload_request(
                    bucket_name, 
                    key, 
                    url, 
                    token, 
                    **request_kwargs, 
                )
            return async_request()
        else:
            if not token:
                token = self.upload_token()
            request_kwargs["headers"] = {
                "x-oss-security-token": token["SecurityToken"], 
                "x-oss-callback": to_base64(callback["callback"]), 
                "x-oss-callback-var": to_base64(callback["callback_var"]), 
            }
            return self._oss_upload_request(
                bucket_name, 
                key, 
                url, 
                token, 
                **request_kwargs, 
            )

    # TODO
    def _oss_upload_future(): ...

    def _oss_multipart_upload(
        self, 
        /, 
        file: ( bytes | bytearray | memoryview | 
                SupportsRead[bytes] | SupportsRead[bytearray] | SupportsRead[memoryview] | 
                Iterable[bytes] | Iterable[bytearray] | Iterable[memoryview] | 
                AsyncIterable[bytes] | AsyncIterable[bytearray] | AsyncIterable[memoryview] ), 
        bucket_name: str, 
        key: str, 
        callback: dict, 
        token: Optional[dict] = None, 
        upload_id: Optional[int] = None, 
        part_size: int = 10 * 1 << 20, # default to: 10 MB
        **request_kwargs, 
    ) -> dict:
        url = self.upload_endpoint_url(bucket_name, key)
        parts: list[dict] = []
        async_ = request_kwargs.get("async_", False)
        if async_:
            async def async_request():
                nonlocal token, upload_id
                if not token:
                    token = await self.upload_token(async_=True)
                if upload_id:
                    async for part in self._oss_multipart_part_iter(
                        bucket_name, key, url, token, upload_id, **request_kwargs, 
                    ):
                        if part["Size"] != part_size:
                            break
                        parts.append(part)
                    skipsize = sum(part["Size"] for part in parts)
                    if skipsize:
                        if isinstance(file, (bytes, bytearray, memoryview)):
                            file = memoryview(file)[skipsize:]
                        elif hasattr(file, "read"):
                            try:
                                ret = await to_thread(file.seek, skipsize) # type: ignore
                                if isawaitable(ret):
                                    await ret
                            except (AttributeError, TypeError, OSError):
                                await async_through(bio_skip_async_iter(file, skipsize))
                        else:
                            file = await bytes_async_iter_skip(file, skipsize)
                else:
                    upload_id = await self._oss_multipart_upload_init(
                        bucket_name, key, url, token, **request_kwargs)
                async for part in self._oss_multipart_upload_part_iter(
                    file, 
                    bucket_name, 
                    key, 
                    url, 
                    token, 
                    upload_id, 
                    part_number_start=len(parts) + 1, 
                    part_size=part_size, 
                    **request_kwargs, 
                ):
                    parts.append(part)
                return await self._oss_multipart_upload_complete(
                    bucket_name, 
                    key, 
                    callback, 
                    url, 
                    token, 
                    upload_id, 
                    parts=parts, 
                    **request_kwargs, 
                )
            return async_request()
        else:
            if not token:
                token = self.upload_token()
            if upload_id:
                parts.extend(takewhile(lambda p: p["Size"] == part_size, self._oss_multipart_part_iter(
                    bucket_name, key, url, token, upload_id, **request_kwargs)))
                skipsize = sum(part["Size"] for part in parts)
                if skipsize:
                    if isinstance(file, (bytes, bytearray, memoryview)):
                        file = memoryview(file)[skipsize:]
                    elif hasattr(file, "read"):
                        try:
                            file.seek(skipsize) # type: ignore
                        except (AttributeError, TypeError, OSError):
                            through(bio_skip_iter(file, skipsize))
                    else:
                        file = bytes_iter_skip(file, skipsize)
            else:
                upload_id = self._oss_multipart_upload_init(
                    bucket_name, key, url, token, **request_kwargs)
            parts.extend(self._oss_multipart_upload_part_iter(
                file, 
                bucket_name, 
                key, 
                url, 
                token, 
                upload_id, 
                part_number_start=len(parts) + 1, 
                part_size=part_size, 
                **request_kwargs, 
            ))
            return self._oss_multipart_upload_complete(
                bucket_name, 
                key, 
                callback, 
                url, 
                token, 
                upload_id, 
                parts=parts, 
                **request_kwargs, 
            )

    # TODO
    def _oss_multipart_upload_future(): ...

    @cached_property
    def upload_info(self, /) -> dict:
        """获取和上传有关的各种服务信息
        GET https://proapi.115.com/app/uploadinfo
        """
        api = "https://proapi.115.com/app/uploadinfo"
        return self.request(api)

    @property
    def user_id(self, /) -> int:
        return self.upload_info["user_id"]

    @property
    def user_key(self, /) -> str:
        return self.upload_info["userkey"]

    @cached_property
    def upload_url(self, /) -> dict:
        """获取用于上传的一些 http 接口，此接口具有一定幂等性，请求一次，然后把响应记下来即可
        GET https://uplb.115.com/3.0/getuploadinfo.php
        response:
            - endpoint: 此接口用于上传文件到阿里云 OSS 
            - gettokenurl: 上传前需要用此接口获取 token
        """
        api = "https://uplb.115.com/3.0/getuploadinfo.php"
        return self.request(api)

    def upload_endpoint_url(self, /, bucket_name, key):
        endpoint = self.upload_url["endpoint"]
        urlp = urlsplit(endpoint)
        return f"{urlp.scheme}://{bucket_name}.{urlp.netloc}/{key}"

    @staticmethod
    def upload_token(**request_kwargs) -> dict:
        """获取阿里云 OSS 的 token，用于上传
        GET https://uplb.115.com/3.0/gettoken.php
        """
        api = "https://uplb.115.com/3.0/gettoken.php"
        request_kwargs.pop("parse", None)
        return request(api, **request_kwargs)

    def upload_file_sample_init(
        self, 
        /, 
        filename: str, 
        pid: int = 0, 
        **request_kwargs, 
    ) -> dict | Awaitable[dict]:
        """网页端的上传接口的初始化，注意：不支持秒传
        POST https://uplb.115.com/3.0/sampleinitupload.php
        """
        api = "https://uplb.115.com/3.0/sampleinitupload.php"
        payload = {"filename": filename, "target": f"U_1_{pid}"}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def upload_file_sample(
        self, 
        /, 
        file: ( str | PathLike | URL | SupportsGeturl | 
                bytes | bytearray | memoryview | 
                SupportsRead[bytes] | SupportsRead[bytearray] | SupportsRead[memoryview] | 
                Iterable[bytes] | Iterable[bytearray] | Iterable[memoryview] | 
                AsyncIterable[bytes] | AsyncIterable[bytearray] | AsyncIterable[memoryview] ), 
        filename: Optional[str] = None, 
        pid: int = 0, 
        **request_kwargs, 
    ) -> dict | Awaitable[dict]:
        """网页端的上传接口，注意：不支持秒传，但也不需要文件大小和 sha1
        """
        async_ = request_kwargs.get("async_", False)
        file_will_open: None | tuple[str, Any] = None
        if isinstance(file, (bytes, bytearray, memoryview)):
            pass
        elif isinstance(file, (str, PathLike)):
            if not filename:
                filename = ospath.basename(fsdecode(file))
            if async_:
                file_will_open = ("path", file)
            else:
                file = open(file, "rb")
        elif hasattr(file, "read"):
            if not async_ and iscoroutinefunction(file.read):
                async_ = request_kwargs["async_"] = True
            if not filename:
                try:
                    filename = ospath.basename(fsdecode(file.name))
                except Exception:
                    pass
        elif isinstance(file, URL) or hasattr(file, "geturl"):
            if isinstance(file, URL):
                url = str(file)
            else:
                url = file.geturl()
            if async_:
                file_will_open = ("url", url)
            else:
                file = urlopen(url)
                if not filename:
                    filename = get_filename(file)
        elif isinstance(file, AsyncIterable):
            if not async_:
                async_ = request_kwargs["async_"] = True
        if async_:
            async def do_request(file, filename):
                if not filename:
                    filename = str(uuid4())
                resp = await self.upload_file_sample_init(filename, pid, **request_kwargs)
                api = resp["host"]
                data = {
                    "name": filename, 
                    "key": resp["object"], 
                    "policy": resp["policy"], 
                    "OSSAccessKeyId": resp["accessid"], 
                    "success_action_status": "200", 
                    "callback": resp["callback"], 
                    "signature": resp["signature"], 
                }
                files = {"file": file}
                headers, request_kwargs["data"] = encode_multipart_data_async(data, files)
                request_kwargs["headers"] = {**request_kwargs.get("headers", {}), **headers}
                return await self.request(api, "POST", **request_kwargs)
            async def async_request():
                if file_will_open:
                    type, path = file_will_open
                    if type == "path":
                        try:
                            from aiofile import async_open
                        except ImportError:
                            with open(path, "rb") as f:
                                return await do_request(f, filename)
                        else:
                            async with async_open(path, "rb") as f:
                                return await do_request(f, filename)
                    elif type == "url":
                        try:
                            from aiohttp import request
                        except ImportError:
                            with (await to_thread(urlopen, url)) as resp:
                                return await do_request(resp, filename or get_filename(resp))
                        else:
                            async with request("GET", url) as resp:
                                return await do_request(resp, filename or get_filename(resp))
                    else:
                        raise ValueError
                return await do_request(file, filename)
            return async_request()
        else:
            if not filename:
                filename = str(uuid4())
            resp = self.upload_file_sample_init(filename, pid, **request_kwargs)
            api = resp["host"]
            data = {
                "name": filename, 
                "key": resp["object"], 
                "policy": resp["policy"], 
                "OSSAccessKeyId": resp["accessid"], 
                "success_action_status": "200", 
                "callback": resp["callback"], 
                "signature": resp["signature"], 
            }
            files = {"file": file}
            headers, request_kwargs["data"] = encode_multipart_data(data, files)
            request_kwargs["headers"] = {**request_kwargs.get("headers", {}), **headers}
            return self.request(api, "POST", **request_kwargs)

    def upload_init(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """秒传接口，参数的构造较为复杂，所以请不要直接使用
        POST https://uplb.115.com/4.0/initupload.php
        """
        api = "https://uplb.115.com/4.0/initupload.php"
        return self.request(api, "POST", **request_kwargs)

    def _upload_file_init(
        self, 
        /, 
        filename: str, 
        filesize: int, 
        file_sha1: str, 
        target: str = "U_1_0", 
        sign_key: str = "", 
        sign_val: str = "", 
        **request_kwargs, 
    ) -> dict | Awaitable[dict]:
        """秒传接口，此接口是对 `upload_init` 的封装
        """
        def gen_sig() -> str:
            sig_sha1 = sha1()
            sig_sha1.update(bytes(userkey, "ascii"))
            sig_sha1.update(b2a_hex(sha1(bytes(f"{userid}{file_sha1}{target}0", "ascii")).digest()))
            sig_sha1.update(b"000000")
            return sig_sha1.hexdigest().upper()
        def gen_token() -> str:
            token_md5 = md5(MD5_SALT)
            token_md5.update(bytes(f"{file_sha1}{filesize}{sign_key}{sign_val}{userid}{t}", "ascii"))
            token_md5.update(b2a_hex(md5(bytes(userid, "ascii")).digest()))
            token_md5.update(bytes(APP_VERSION, "ascii"))
            return token_md5.hexdigest()
        userid = str(self.user_id)
        userkey = self.user_key
        t = int(time())
        sig = gen_sig()
        token = gen_token()
        encoded_token = ECDH_ENCODER.encode_token(t).decode("ascii")
        data = {
            "appid": 0, 
            "appversion": APP_VERSION, 
            "userid": userid, 
            "filename": filename, 
            "filesize": filesize, 
            "fileid": file_sha1, 
            "target": target, 
            "sig": sig, 
            "t": t, 
            "token": token, 
        }
        if sign_key and sign_val:
            data["sign_key"] = sign_key
            data["sign_val"] = sign_val
        if (headers := request_kwargs.get("headers")):
            request_kwargs["headers"] = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
        else:
            request_kwargs["headers"] = {"Content-Type": "application/x-www-form-urlencoded"}
        request_kwargs["parse"] = lambda resp, content: loads(ECDH_ENCODER.decode(content))
        request_kwargs["params"] = {"k_ec": encoded_token}
        request_kwargs["data"] = ECDH_ENCODER.encode(urlencode(sorted(data.items())))
        return self.upload_init(**request_kwargs)

    def upload_file_init(
        self, 
        /, 
        filename: str, 
        filesize: int, 
        file_sha1: str | Callable[[], str], 
        read_range_bytes_or_hash: None | Callable[[str], str | bytes | bytearray | memoryview] = None, 
        pid: int = 0, 
        **request_kwargs, 
    ) -> dict:
        """秒传接口，此接口是对 `upload_init` 的封装。
        NOTE: 
            - 文件大小 和 sha1 是必需的，只有 sha1 是没用的。
            - 如果文件大于等于 1 MB (1048576 B)，就需要 2 次检验一个范围哈希，就必须提供 `read_range_bytes_or_hash`
        """
        if filesize >= 1 << 20:
            raise ValueError("filesize >= 1 MB, thus need pass the `read_range_bytes_or_hash` argument")

        async_ = request_kwargs.get("async_", False)
        if async_:
            async def async_request():
                nonlocal file_sha1
                if not isinstance(file_sha1, str):
                    file_sha1 = await to_thread(file_sha1)
                    if isawaitable(file_sha1):
                        file_sha1 = await file_sha1
                    file_sha1 = cast(str, file_sha1)
                file_sha1 = file_sha1.upper()
                target = f"U_1_{pid}"
                resp = await self._upload_file_init(
                    filename, 
                    filesize, 
                    file_sha1, 
                    target, 
                    **request_kwargs, 
                )
                if resp["status"] == 7 and resp["statuscode"] == 701:
                    sign_key = resp["sign_key"]
                    sign_check = resp["sign_check"]
                    data = await ensure_async(read_range_bytes_or_hash)(sign_check)
                    if isinstance(data, str):
                        sign_val = data.upper()
                    else:
                        sign_val = sha1(data).hexdigest().upper()
                    resp = await self._upload_file_init(
                        filename, 
                        filesize, 
                        file_sha1, 
                        target, 
                        sign_key=sign_key, 
                        sign_val=sign_val, 
                        **request_kwargs, 
                    )
                resp["state"] = True
                resp["data"] = {
                    "file_name": filename, 
                    "file_size": filesize, 
                    "sha1": file_sha1, 
                    "cid": pid, 
                    "pickcode": resp["pickcode"], 
                }
                return resp
            return async_request()
        else:
            if not isinstance(file_sha1, str):
                file_sha1 = file_sha1()
            file_sha1 = file_sha1.upper()
            target = f"U_1_{pid}"
            resp = self._upload_file_init(
                filename, 
                filesize, 
                file_sha1, 
                target, 
                **request_kwargs, 
            )
            # NOTE: 当文件大于等于 1 MB (1048576 B)，需要 2 次检验 1 个范围哈希，它会给出此文件的 1 个范围区间
            #       ，你读取对应的数据计算 sha1 后上传，以供 2 次检验
            if resp["status"] == 7 and resp["statuscode"] == 701:
                sign_key = resp["sign_key"]
                sign_check = resp["sign_check"]
                data = read_range_bytes_or_hash(sign_check)
                if isinstance(data, str):
                    sign_val = data.upper()
                else:
                    sign_val = sha1(data).hexdigest().upper()
                resp = self._upload_file_init(
                    filename, 
                    filesize, 
                    file_sha1, 
                    target, 
                    sign_key=sign_key, 
                    sign_val=sign_val, 
                    **request_kwargs, 
                )
            resp["state"] = True
            resp["data"] = {
                "file_name": filename, 
                "file_size": filesize, 
                "sha1": file_sha1, 
                "cid": pid, 
                "pickcode": resp["pickcode"], 
            }
            return resp

    def upload_file(
        self, 
        /, 
        file: ( str | PathLike | URL | SupportsGeturl | 
                bytes | bytearray | memoryview | 
                SupportsRead[bytes] | SupportsRead[bytearray] | SupportsRead[memoryview] | 
                Iterable[bytes] | Iterable[bytearray] | Iterable[memoryview] | 
                AsyncIterable[bytes] | AsyncIterable[bytearray] | AsyncIterable[memoryview] ), 
        filename: Optional[str] = None, 
        pid: int = 0, 
        filesize: int = -1, 
        file_sha1: Optional[str] = None, 
        part_size: int = 0, 
        upload_directly: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """文件上传接口，这是高层封装，推荐使用
        """
        if upload_directly:
            return self.upload_file_sample(file, filename, pid, **request_kwargs)

        async_ = request_kwargs.get("async_", False)
        if isinstance(file, (bytes, bytearray, memoryview, str, PathLike)):
            pass
        elif hasattr(file, "read"):
            if not async_ and iscoroutinefunction(file.read):
                async_ = request_kwargs["async_"] = True
        elif not async_ and isinstance(file, AsyncIterable):
            async_ = request_kwargs["async_"] = True

        if async_:
            async def async_request():
                nonlocal file, filename, filesize, file_sha1

                async def do_upload(file):
                    resp = await self.upload_file_init(
                        filename, 
                        filesize, 
                        file_sha1, 
                        read_range_bytes_or_hash, 
                        pid=pid, 
                        **request_kwargs, 
                    )
                    status = resp["status"]
                    statuscode = resp.get("statuscode", 0)
                    if status == 2 and statuscode == 0:
                        return resp
                    elif status == 1 and statuscode == 0:
                        bucket_name, key, callback = resp["bucket"], resp["object"], resp["callback"]
                    else:
                        raise OSError(errno.EINVAL, resp)

                    if part_size <= 0:
                        return await self._oss_upload(
                            file, 
                            bucket_name, 
                            key, 
                            callback, 
                            **request_kwargs, 
                        )
                    else:
                        return await self._oss_multipart_upload(
                            file, 
                            bucket_name, 
                            key, 
                            callback, 
                            part_size=part_size, 
                            **request_kwargs, 
                        )

                read_range_bytes_or_hash = None
                if isinstance(file, (bytes, bytearray, memoryview)):
                    if filesize < 0:
                        filesize = len(file)
                    if not file_sha1:
                        file_sha1 = sha1(file).hexdigest()
                    if filesize >= 1 << 20:
                        def read_range_bytes_or_hash(sign_check):
                            start, end = map(int, sign_check.split("-"))
                            return data[start : end + 1]
                elif isinstance(file, (str, PathLike)):
                    @asynccontextmanager
                    async def ctx_async_read(path, /, start=0):
                        try:
                            from aiofile import async_open
                        except ImportError:
                            with open(path, "rb") as file:
                                if start:
                                    file.seek(file)
                                yield file, as_thread(file.read)
                        else:
                            async with async_open(path, "rb") as file:
                                if start:
                                    await file.seek(start)
                                yield file, file.read
                    path = fsdecode(file)
                    if not filename:
                        filename = ospath.basename(path)
                    if filesize < 0:
                        filesize = stat(path).st_size
                    if filesize < 1 << 20:
                        async with ctx_async_read(path) as _, read:
                            file = await read()
                        if not file_sha1:
                            file_sha1 = sha1(file).hexdigest()
                    else:
                        if not file_sha1:
                            h = sha1()
                            h_update = h.update
                            async with ctx_async_read(path) as _, read:
                                while (chunk := (await read(1 << 16))):
                                    h_update(chunk)
                            file_sha1 = h.hexdigest()
                        async def read_range_bytes_or_hash(sign_check):
                            start, end = map(int, sign_check.split("-"))
                            async with ctx_async_read(path, start) as _, read:
                                return await file.read(end - start + 1)
                        async with ctx_async_read(path) as file, _:
                            return await do_upload(file)
                elif hasattr(file, "read"):
                    try:
                        file_seek = ensure_async(file.seek)
                        curpos = await file_seek(0, 1)
                        seekable = True
                    except Exception:
                        curpos = 0
                        seekable = False
                    file_read = ensure_async(file.read)
                    if not filename:
                        try:
                            filename = ospath.basename(fsdecode(file.name))
                        except Exception:
                            filename = str(uuid4())
                    if filesize < 0:
                        try:
                            filesize = fstat(file.fileno()).st_size - curpos
                        except Exception:
                            try:
                                filesize = len(file) - curpos
                            except TypeError:
                                if seekable:
                                    try:
                                        filesize = (await file_seek(0, 2)) - curpos
                                    finally:
                                        await file_seek(curpos)
                                else:
                                    filesize = 0
                    if 0 < filesize <= 1 << 20:
                        file = await file_read()
                        if not file_sha1:
                            file_sha1 = sha1(file).hexdigest()
                    else:
                        if not file_sha1:
                            if not seekable:
                                return await self.upload_file_sample(file, filename, pid, **request_kwargs)
                            async def file_sha1(): 
                                try:
                                    h = sha1()
                                    h_update = h.update
                                    while (chunk := (await file_read(1 << 16))):
                                        h_update(chunk)
                                    return h.hexdigest()
                                finally:
                                    await file_seek(curpos)
                        async def read_range_bytes_or_hash(sign_check):
                            if not seekable:
                                raise TypeError(f"not a seekable reader: {file!r}")
                            start, end = map(int, sign_check.split("-"))
                            try:
                                file_read = ensure_async(file.read)
                                await file_seek(start)
                                return await file_read(end - start + 1)
                            finally:
                                await file_seek(curpos)
                elif isinstance(file, URL) or hasattr(file, "geturl"):
                    @asynccontextmanager
                    async def ctx_async_read(url, /, start=0):
                        headers = None
                        if start and is_ranged_url:
                            headers = {"Range": "bytes=%s-" % start}
                        try:
                            from aiohttp import request
                        except ImportError:
                            with (await to_thread(urlopen, url, headers=headers)) as resp:
                                if not headers:
                                    await async_through(bio_skip_async_iter(resp, start))
                                yield resp, as_thread(resp.read)
                        else:
                            async with request("GET", url, headers=headers) as resp:
                                if not headers:
                                    await async_through(bio_skip_async_iter(resp, start))
                                yield resp, resp.read
                    async def read_range_bytes_or_hash(sign_check):
                        start, end = map(int, sign_check.split("-"))
                        async with ctx_async_read(url, start) as _, read:
                            return await read(end - start + 1)
                    if isinstance(file, URL):
                        url = str(file)
                    else:
                        url = file.geturl()
                    async with ctx_async_read(url) as resp, read:
                        is_ranged_url = is_range_request(resp)
                        if not filename:
                            filename = get_filename(resp) or str(uuid4())
                        if filesize < 0:
                            filesize = get_total_length(resp) or 0
                        if filesize < 1 << 20:
                            file = await read()
                            if not file_sha1:
                                file_sha1 = sha1(file).hexdigest()
                        else:
                            if not file_sha1 or not is_ranged_url:
                                return await self.upload_file_sample(resp, filename, pid, **request_kwargs)
                            return await do_upload(resp)
                else:
                    return await self.upload_file_sample(file, filename, pid, **request_kwargs)

                if not filename:
                    filename = str(uuid4())

                return await do_upload(file)

            return async_request()
        else:
            def do_upload(file):
                resp = self.upload_file_init(
                    filename, 
                    filesize, 
                    file_sha1, 
                    read_range_bytes_or_hash, 
                    pid=pid, 
                    **request_kwargs, 
                )
                status = resp["status"]
                statuscode = resp.get("statuscode", 0)
                if status == 2 and statuscode == 0:
                    return resp
                elif status == 1 and statuscode == 0:
                    bucket_name, key, callback = resp["bucket"], resp["object"], resp["callback"]
                else:
                    raise OSError(errno.EINVAL, resp)

                if part_size <= 0:
                    return self._oss_upload(
                        file, 
                        bucket_name, 
                        key, 
                        callback, 
                        **request_kwargs, 
                    )
                else:
                    return self._oss_multipart_upload(
                        file, 
                        bucket_name, 
                        key, 
                        callback, 
                        part_size=part_size, 
                        **request_kwargs, 
                    )

            read_range_bytes_or_hash = None
            if isinstance(file, (bytes, bytearray, memoryview)):
                if filesize < 0:
                    filesize = len(file)
                if not file_sha1:
                    file_sha1 = sha1(file).hexdigest()
                if filesize >= 1 << 20:
                    def read_range_bytes_or_hash(sign_check: str) -> str:
                        start, end = map(int, sign_check.split("-"))
                        return data[start : end + 1]
            elif isinstance(file, (str, PathLike)):
                path = fsdecode(file)
                if not filename:
                    filename = ospath.basename(path)
                if filesize < 0:
                    filesize = stat(path).st_size
                if filesize < 1 << 20:
                    file = open(path, "rb", buffering=0).read()
                    if not file_sha1:
                        file_sha1 = sha1(file).hexdigest()
                else:
                    if not file_sha1:
                        file_sha1 = file_digest(open(path, "rb"))
                    def read_range_bytes_or_hash(sign_check):
                        start, end = map(int, sign_check.split("-"))
                        with open(path, "rb") as file:
                            file.seek(start)
                            return sha1(file.read(end - start + 1)).hexdigest()
                    file = open(path, "rb")
            elif hasattr(file, "read"):
                try:
                    curpos = file.seek(0, 1)
                    seekable = True
                except Exception:
                    curpos = 0
                    seekable = False
                if not filename:
                    try:
                        filename = ospath.basename(fsdecode(file.name))
                    except Exception:
                        filename = str(uuid4())
                if filesize < 0:
                    try:
                        filesize = fstat(file.fileno()).st_size - curpos
                    except Exception:
                        try:
                            filesize = len(file) - curpos
                        except TypeError:
                            if seekable:
                                try:
                                    filesize = file.seek(0, 2) - curpos
                                finally:
                                    file.seek(curpos)
                            else:
                                filesize = 0
                if 0 < filesize < 1 << 20:
                    file = file.read()
                    if not file_sha1:
                        file_sha1 = sha1(file).hexdigest()
                else:
                    if not file_sha1:
                        if not seekable:
                            return self.upload_file_sample(file, filename, pid, **request_kwargs)
                        def file_sha1():
                            try:
                                return file_digest(file, "sha1").hexdigest()
                            finally:
                                file.seek(curpos)
                    def read_range_bytes_or_hash(sign_check):
                        if not seekable:
                            raise TypeError(f"not a seekable reader: {file!r}")
                        start, end = map(int, sign_check.split("-"))
                        try:
                            file.seek(start)
                            return sha1(file.read(end - start + 1)).hexdigest()
                        finally:
                            file.seek(curpos)
            elif isinstance(file, URL) or hasattr(file, "geturl"):
                def read_range_bytes_or_hash(sign_check):
                    start, end = map(int, sign_check.split("-"))
                    headers = None
                    if is_range_request and start:
                        headers = {"Range": "bytes=%s-" % start}
                    with urlopen(url, headers=headers) as resp:
                        if not headers:
                            through(bio_skip_iter(resp, start))
                        return resp.read(end - start + 1)
                if isinstance(file, URL):
                    url = str(file)
                else:
                    url = file.geturl()
                with urlopen(url) as resp:
                    is_ranged_url = is_range_request(resp)
                    if not filename:
                        filename = get_filename(resp) or str(uuid4())
                    if filesize < 0:
                        filesize = resp.length or 0
                    if 0 < filesize < 1 << 20:
                        file = resp.read()
                        if not file_sha1:
                            file_sha1 = sha1(file).hexdigest()
                    else:
                        if not file_sha1 or not is_ranged_url:
                            return self.upload_file_sample(resp, filename, pid, **request_kwargs)
                        return do_upload(resp)
            else:
                return self.upload_file_sample(file, filename, pid, **request_kwargs)

            if not filename:
                filename = str(uuid4())

            return do_upload(file)

    # TODO: 提供一个可断点续传的版本
    # TODO: 支持进度条
    # TODO: 返回 future，支持 pause（暂停此任务，连接不释放）、stop（停止此任务，连接释放）、cancel（取消此任务）、resume（恢复），此时需要增加参数 wait
    def upload_file_future(self):
        ...

    ########## Decompress API ##########

    def extract_push(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """推送一个解压缩任务给服务器，完成后，就可以查看压缩包的文件列表了
        POST https://webapi.115.com/files/push_extract
        payload:
            - pick_code: str
            - secret: str = "" # 解压密码
        """
        api = "https://webapi.115.com/files/push_extract"
        if isinstance(payload, str):
            payload = {"pick_code": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def extract_push_progress(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """查询解压缩任务的进度
        GET https://webapi.115.com/files/push_extract
        payload:
            - pick_code: str
        """
        api = "https://webapi.115.com/files/push_extract"
        if isinstance(payload, str):
            payload = {"pick_code": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def extract_info(
        self, 
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取压缩文件的文件列表，推荐直接用封装函数 `extract_list`
        GET https://webapi.115.com/files/extract_info
        payload:
            - pick_code: str
            - file_name: str
            - paths: str
            - next_marker: str
            - page_count: int | str # NOTE: 介于 1-999
        """
        api = "https://webapi.115.com/files/extract_info"
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def extract_list(
        self, 
        /, 
        pickcode: str, 
        path: str = "", 
        next_marker: str = "", 
        page_count: int = 999, 
        **request_kwargs, 
    ) -> dict:
        """获取压缩文件的文件列表，此方法是对 `extract_info` 的封装，推荐使用
        """
        if not 1 <= page_count <= 999:
            page_count = 999
        payload = {
            "pick_code": pickcode, 
            "file_name": path.strip("/"), 
            "paths": "文件", 
            "next_marker": next_marker, 
            "page_count": page_count, 
        }
        return self.extract_info(payload, **request_kwargs)

    def extract_add_file(
        self, 
        payload: list | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """解压缩到某个文件夹，推荐直接用封装函数 `extract_file`
        POST https://webapi.115.com/files/add_extract_file
        payload:
            - pick_code: str
            - extract_file[]: str
            - extract_file[]: str
            - ...
            - to_pid: int | str = 0
            - paths: str = "文件"
        """
        api = "https://webapi.115.com/files/add_extract_file"
        if (headers := request_kwargs.get("headers")):
            headers = request_kwargs["headers"] = dict(headers)
        else:
            headers = request_kwargs["headers"] = {}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        request_kwargs.pop("parse", None)
        return self.request(
            api, 
            "POST", 
            data=urlencode(payload), 
            **request_kwargs, 
        )

    def extract_progress(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取 解压缩到文件夹 任务的进度
        GET https://webapi.115.com/files/add_extract_file
        payload:
            - extract_id: str
        """
        api = "https://webapi.115.com/files/add_extract_file"
        if isinstance(payload, (int, str)):
            payload = {"extract_id": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def extract_file(
        self, 
        /, 
        pickcode: str, 
        paths: str | Sequence[str] = "", 
        dirname: str = "", 
        to_pid: int | str = 0, 
        **request_kwargs, 
    ) -> dict:
        """解压缩到某个文件夹，是对 `extract_add_file` 的封装，推荐使用
        """
        dirname = dirname.strip("/")
        dir2 = f"文件/{dirname}" if dirname else "文件"
        data = [
            ("pick_code", pickcode), 
            ("paths", dir2), 
            ("to_pid", to_pid), 
        ]
        if not paths:
            resp = self.extract_list(pickcode, dirname)
            if not resp["state"]:
                return resp
            paths = [p["file_name"] if p["file_category"] else p["file_name"]+"/" for p in resp["data"]["list"]]
            while (next_marker := resp["data"].get("next_marker")):
                resp = self.extract_list(pickcode, dirname, next_marker)
                paths.extend(p["file_name"] if p["file_category"] else p["file_name"]+"/" for p in resp["data"]["list"])
        if isinstance(paths, str):
            data.append(("extract_dir[]" if paths.endswith("/") else "extract_file[]", paths.strip("/")))
        else:
            data.extend(("extract_dir[]" if path.endswith("/") else "extract_file[]", path.strip("/")) for path in paths)
        return self.extract_add_file(data, **request_kwargs)

    def extract_download_url_web(
        self, 
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取压缩包中文件的下载链接
        GET https://webapi.115.com/files/extract_down_file
        payload:
            - pick_code: str
            - full_name: str
        """
        api = "https://webapi.115.com/files/extract_down_file"
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def extract_download_url(
        self, 
        /, 
        pickcode: str, 
        path: str, 
        **request_kwargs, 
    ) -> str:
        """获取压缩包中文件的下载链接，此接口是对 `extract_download_url_web` 的封装
        """
        async_ = request_kwargs.get("async_", False)
        resp = self.extract_download_url_web(
            {"pick_code": pickcode, "full_name": path.strip("/")}, 
            **request_kwargs, 
        )
        def get_url(resp: dict) -> str:
            data = check_response(resp)["data"]
            return quote(data["url"], safe=":/?&=%#")
        if async_:
            async def request() -> str:
                return get_url(await resp) # type: ignore
            return request() # type: ignore
        else:
            return get_url(resp)

    def extract_push_future(
        self, 
        /, 
        pickcode: str, 
        secret: str = "", 
        **request_kwargs, 
    ) -> Optional[PushExtractProgress]:
        """执行在线解压，如果早就已经完成，返回 None，否则新开启一个线程，用于检查进度
        """
        resp = check_response(self.extract_push(
            {"pick_code": pickcode, "secret": secret}, **request_kwargs
        ))
        if resp["data"]["unzip_status"] == 4:
            return None
        return PushExtractProgress(self, pickcode)

    def extract_file_future(
        self, 
        /, 
        pickcode: str, 
        paths: str | Sequence[str] = "", 
        dirname: str = "", 
        to_pid: int | str = 0, 
        **request_kwargs, 
    ) -> ExtractProgress:
        """执行在线解压到目录，新开启一个线程，用于检查进度
        """
        resp = check_response(self.extract_file(
            pickcode, paths, dirname, to_pid, **request_kwargs
        ))
        return ExtractProgress(self, resp["data"]["extract_id"])

    ########## Offline Download API ##########

    def offline_info(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取关于离线的限制的信息
        GET https://115.com/?ct=offline&ac=space
        """
        api = "https://115.com/?ct=offline&ac=space"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def offline_quota_info(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取当前离线配额信息（简略）
        GET https://lixian.115.com/lixian/?ct=lixian&ac=get_quota_info
        """
        api = "https://lixian.115.com/lixian/?ct=lixian&ac=get_quota_info"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def offline_quota_package_info(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取当前离线配额信息（详细）
        GET https://lixian.115.com/lixian/?ct=lixian&ac=get_quota_package_info
        """
        api = "https://lixian.115.com/lixian/?ct=lixian&ac=get_quota_package_info"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def offline_download_path(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取当前默认的离线下载到的文件夹信息（可能有多个）
        GET https://webapi.115.com/offine/downpath
        """
        api = "https://webapi.115.com/offine/downpath"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def offline_upload_torrent_path(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取当前的种子上传到的文件夹，当你添加种子任务后，这个种子会在此文件夹中保存
        GET https://115.com/?ct=lixian&ac=get_id&torrent=1
        """
        api = "https://115.com/?ct=lixian&ac=get_id&torrent=1"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def offline_add_url(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """添加一个离线任务
        POST https://115.com/web/lixian/?ct=lixian&ac=add_task_url
        payload:
            - url: str
            - sign: str = <default>
            - time: int = <default>
            - savepath: str = <default>
            - wp_path_id: int | str = <default>
        """
        api = "https://115.com/web/lixian/?ct=lixian&ac=add_task_url"
        if isinstance(payload, str):
            payload = {"url": payload}
        if "sign" not in payload:
            info = self.offline_info()
            payload["sign"] = info["sign"]
            payload["time"] = info["time"]
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def offline_add_urls(
        self, 
        payload: Iterable[str] | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """添加一组离线任务
        POST https://115.com/web/lixian/?ct=lixian&ac=add_task_urls
        payload:
            - url[0]: str
            - url[1]: str
            - ...
            - sign: str = <default>
            - time: int = <default>
            - savepath: str = <default>
            - wp_path_id: int | str = <default>
        """
        api = "https://115.com/web/lixian/?ct=lixian&ac=add_task_urls"
        if not isinstance(payload, dict):
            payload = {f"url[{i}]": url for i, url in enumerate(payload)}
            if not payload:
                raise ValueError("no `url` specified")
        if "sign" not in payload:
            info = self.offline_info()
            payload["sign"] = info["sign"]
            payload["time"] = info["time"]
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def offline_add_torrent(
        self, 
        payload: dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """添加一个种子作为离线任务
        POST https://115.com/web/lixian/?ct=lixian&ac=add_task_bt
        payload:
            - info_hash: str
            - wanted: str
            - sign: str = <default>
            - time: int = <default>
            - savepath: str = <default>
            - wp_path_id: int | str = <default>
        """
        api = "https://115.com/web/lixian/?ct=lixian&ac=add_task_bt"
        if "sign" not in payload:
            info = self.offline_info()
            payload["sign"] = info["sign"]
            payload["time"] = info["time"]
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def offline_torrent_info(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """查看种子的文件列表等信息
        POST https://lixian.115.com/lixian/?ct=lixian&ac=torrent
        payload:
            - sha1: str
        """
        api = "https://lixian.115.com/lixian/?ct=lixian&ac=torrent"
        if isinstance(payload, str):
            payload = {"sha1": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def offline_remove(
        self, 
        payload: str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """删除一组离线任务（无论是否已经完成）
        POST https://lixian.115.com/lixian/?ct=lixian&ac=task_del
        payload:
            - hash[0]: str
            - hash[1]: str
            - ...
            - sign: str = <default>
            - time: int = <default>
            - flag: 0 | 1 = <default> # 是否删除源文件
        """
        api = "https://lixian.115.com/lixian/?ct=lixian&ac=task_del"
        if isinstance(payload, str):
            payload = {"hash[0]": payload}
        if "sign" not in payload:
            info = self.offline_info()
            payload["sign"] = info["sign"]
            payload["time"] = info["time"]
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def offline_list(
        self, 
        payload: int | dict = 1, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取当前的离线任务列表
        POST https://lixian.115.com/lixian/?ct=lixian&ac=task_lists
        payload:
            - page: int | str
        """
        api = "https://lixian.115.com/lixian/?ct=lixian&ac=task_lists"
        if isinstance(payload, int):
            payload = {"page": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def offline_clear(
        self, 
        payload: int | dict = {"flag": 0}, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """清空离线任务列表
        POST https://115.com/web/lixian/?ct=lixian&ac=task_clear
        payload:
            flag: int = 0
                - 0: 已完成
                - 1: 全部
                - 2: 已失败
                - 3: 进行中
                - 4: 已完成+删除源文件
                - 5: 全部+删除源文件
        """
        api = "https://115.com/web/lixian/?ct=lixian&ac=task_clear"
        if isinstance(payload, int):
            flag = payload
            if flag < 0:
                flag = 0
            elif flag > 5:
                flag = 5
            payload = {"flag": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    ########## Recyclebin API ##########

    def recyclebin_info(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """回收站：文件信息
        POST https://webapi.115.com/rb/rb_info
        payload:
            - rid: int | str
        """
        api = "https://webapi.115.com/rb/rb_info"
        if isinstance(payload, (int, str)):
            payload = {"rid": payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def recyclebin_clean(
        self, 
        payload: int | str | Iterable[int | str] | dict = {}, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """回收站：删除或清空
        POST https://webapi.115.com/rb/clean
        payload:
            - rid[0]: int | str # NOTE: 如果没有 rid，就是清空回收站
            - rid[1]: int | str
            - ...
            - password: int | str = <default>
        """
        api = "https://webapi.115.com/rb/clean"
        if isinstance(payload, (int, str)):
            payload = {"rid[0]": payload}
        elif not isinstance(payload, dict):
            payload = {f"rid[{i}]": rid for i, rid in enumerate(payload)}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    def recyclebin_list(
        self, 
        payload: dict = {"limit": 32, "offset": 0}, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """回收站：罗列
        GET https://webapi.115.com/rb
        payload:
            - aid: int | str = 7
            - cid: int | str = 0
            - limit: int = 32
            - offset: int = 0
            - format: str = "json"
            - source: str = <default>
        """ 
        api = "https://webapi.115.com/rb"
        payload = {"aid": 7, "cid": 0, "limit": 32, "offset": 0, "format": "json", **payload}
        request_kwargs.pop("parse", None)
        return self.request(api, params=payload, **request_kwargs)

    def recyclebin_revert(
        self, 
        payload: int | str | Iterable[int | str] | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """回收站：还原
        POST https://webapi.115.com/rb/revert
        payload:
            - rid[0]: int | str
            - rid[1]: int | str
            - ...
        """
        api = "https://webapi.115.com/rb/revert"
        if isinstance(payload, (int, str)):
            payload = {"rid[0]": payload}
        elif not isinstance(payload, dict):
            payload = {f"rid[{i}]": rid for i, rid in enumerate(payload)}
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    ########## Captcha System API ##########

    def captcha_sign(
        self, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """获取验证码的签名字符串
        GET https://captchaapi.115.com/?ac=code&t=sign
        """
        api = "https://captchaapi.115.com/?ac=code&t=sign"
        request_kwargs.pop("parse", None)
        return self.request(api, **request_kwargs)

    def captcha_code(
        self, 
        /, 
        **request_kwargs, 
    ) -> bytes:
        """更新验证码，并获取图片数据（含 4 个汉字）
        GET https://captchaapi.115.com/?ct=index&ac=code&ctype=0
        """
        api = "https://captchaapi.115.com/?ct=index&ac=code&ctype=0"
        request_kwargs["parse"] = False
        return self.request(api, **request_kwargs)

    def captcha_all(
        self, 
        /, 
        **request_kwargs, 
    ) -> bytes:
        """返回一张包含 10 个汉字的图片，包含验证码中 4 个汉字（有相应的编号，从 0 到 9，计数按照从左到右，从上到下的顺序）
        GET https://captchaapi.115.com/?ct=index&ac=code&t=all
        """
        api = "https://captchaapi.115.com/?ct=index&ac=code&t=all"
        request_kwargs["parse"] = False
        return self.request(api, **request_kwargs)

    def captcha_single(
        self, 
        id: int, 
        /, 
        **request_kwargs, 
    ) -> bytes:
        """10 个汉字单独的图片，包含验证码中 4 个汉字，编号从 0 到 9
        GET https://captchaapi.115.com/?ct=index&ac=code&t=single&id={id}
        """
        if not 0 <= id <= 9:
            raise ValueError(f"expected integer between 0 and 9, got {id}")
        api = f"https://captchaapi.115.com/?ct=index&ac=code&t=single&id={id}"
        request_kwargs["parse"] = False
        return self.request(api, **request_kwargs)

    def captcha_verify(
        self, 
        payload: int | str | dict, 
        /, 
        **request_kwargs, 
    ) -> dict:
        """提交验证码
        POST https://webapi.115.com/user/captcha
        payload:
            - code: int | str # 从 0 到 9 中选取 4 个数字的一种排列
            - sign: str = <default>
            - ac: str = "security_code"
            - type: str = "web"
        """
        if isinstance(payload, (int, str)):
            payload = {"code": payload, "ac": "security_code", "type": "web"}
        else:
            payload = {"ac": "security_code", "type": "web", **payload}
        if "sign" not in payload:
            payload["sign"] = self.captcha_sign()["sign"]
        api = "https://webapi.115.com/user/captcha"
        request_kwargs.pop("parse", None)
        return self.request(api, "POST", data=payload, **request_kwargs)

    ########## Other Encapsulations ##########

    # TODO: 支持 async_
    def open(
        self, 
        /, 
        url: str | Callable[[], str], 
        headers: Optional[Mapping] = None, 
        start: int = 0, 
        seek_threshold: int = 1 << 20, 
        **request_kwargs, 
    ) -> RequestsFileReader:
        """
        """
        urlopen = self.session.get
        if request_kwargs:
            urlopen = partial(urlopen, **request_kwargs)
        return RequestsFileReader(
            url, 
            headers=headers, 
            start=start, 
            seek_threshold=seek_threshold, 
            urlopen=urlopen, 
        )

    # TODO: 返回一个 HTTPFileWriter，随时可以写入一些数据，close 代表上传完成，这个对象会持有一些信息
    def open_upload(): ...

    # TODO: 下面 3 个函数支持 async_
    def read_bytes(
        self, 
        /, 
        url: str, 
        start: int = 0, 
        stop: Optional[int] = None, 
        headers: Optional[Mapping] = None, 
        **request_kwargs, 
    ) -> bytes:
        """
        """
        length = None
        if start < 0:
            with self.session.get(url, stream=True, headers={"Accept-Encoding": "identity"}) as resp:
                resp.raise_for_status()
                length = get_content_length(resp)
            if length is None:
                raise OSError(errno.ESPIPE, "can't determine content length")
            start += length
        if start < 0:
            start = 0
        if stop is None:
            bytes_range = f"{start}-"
        else:
            if stop < 0:
                if length is None:
                    with self.session.get(url, stream=True, headers={"Accept-Encoding": "identity"}) as resp:
                        resp.raise_for_status()
                        length = get_content_length(resp)
                if length is None:
                    raise OSError(errno.ESPIPE, "can't determine content length")
                stop += length
            if stop <= 0 or start >= stop:
                return b""
            bytes_range = f"{start}-{stop-1}"
        return self.read_bytes_range(url, bytes_range, headers=headers, **request_kwargs)

    def read_bytes_range(
        self, 
        /, 
        url: str, 
        bytes_range: str = "0-", 
        headers: Optional[Mapping] = None, 
        **request_kwargs, 
    ) -> bytes:
        """
        """
        if headers:
            headers = {**headers, "Accept-Encoding": "identity", "Range": f"bytes={bytes_range}"}
        else:
            headers = {"Accept-Encoding": "identity", "Range": f"bytes={bytes_range}"}
        request_kwargs["stream"] = False
        with self.session.get(url, headers=headers, **request_kwargs) as resp:
            if resp.status_code == 416:
                return b""
            resp.raise_for_status()
            return resp.content

    def read_block(
        self, 
        /, 
        url: str, 
        size: int = 0, 
        offset: int = 0, 
        headers: Optional[Mapping] = None, 
        **request_kwargs, 
    ) -> bytes:
        """
        """
        if size <= 0:
            return b""
        return self.read_bytes(url, offset, offset+size, headers=headers, **request_kwargs)


# TODO: 这些类再提供一个 Async 版本
class ExportDirStatus(Future):
    _condition: Condition
    _state: str

    def __init__(self, /, client: P115Client, export_id: int | str):
        super().__init__()
        self.status = 0
        self.set_running_or_notify_cancel()
        self._run_check(client, export_id)

    def __bool__(self, /) -> bool:
        return self.status == 1

    def __del__(self, /):
        self.stop()

    def stop(self, /):
        with self._condition:
            if self._state in ["RUNNING", "PENDING"]:
                self._state = "CANCELLED"
                self.set_exception(OSError(errno.ECANCELED, "canceled"))

    def _run_check(self, client, export_id: int | str, /):
        check = check_response(client.fs_export_dir_status)
        payload = {"export_id": export_id}
        def update_progress():
            while self.running():
                try:
                    data = check(payload)["data"]
                    if data:
                        self.status = 1
                        self.set_result(data)
                        return
                except BaseException as e:
                    self.set_exception(e)
                    return
                sleep(1)
        Thread(target=update_progress).start()


class PushExtractProgress(Future):
    _condition: Condition
    _state: str

    def __init__(self, /, client: P115Client, pickcode: str):
        super().__init__()
        self.progress = 0
        self.set_running_or_notify_cancel()
        self._run_check(client, pickcode)

    def __del__(self, /):
        self.stop()

    def __bool__(self, /) -> bool:
        return self.progress == 100

    def stop(self, /):
        with self._condition:
            if self._state in ["RUNNING", "PENDING"]:
                self._state = "CANCELLED"
                self.set_exception(OSError(errno.ECANCELED, "canceled"))

    def _run_check(self, client, pickcode: str, /):
        check = check_response(client.extract_push_progress)
        payload = {"pick_code": pickcode}
        def update_progress():
            while self.running():
                try:
                    data = check(payload)["data"]
                    extract_status = data["extract_status"]
                    progress = extract_status["progress"]
                    if progress == 100:
                        self.set_result(data)
                        return
                    match extract_status["unzip_status"]:
                        case 1 | 2 | 4:
                            self.progress = progress
                        case 0:
                            raise OSError(errno.EIO, f"bad file format: {data!r}")
                        case 6:
                            raise OSError(errno.EINVAL, f"wrong password/secret: {data!r}")
                        case _:
                            raise OSError(errno.EIO, f"undefined error: {data!r}")
                except BaseException as e:
                    self.set_exception(e)
                    return
                sleep(1)
        Thread(target=update_progress).start()


class ExtractProgress(Future):
    _condition: Condition
    _state: str

    def __init__(self, /, client: P115Client, extract_id: int | str):
        super().__init__()
        self.progress = 0
        self.set_running_or_notify_cancel()
        self._run_check(client, extract_id)

    def __del__(self, /):
        self.stop()

    def __bool__(self, /) -> bool:
        return self.progress == 100

    def stop(self, /):
        with self._condition:
            if self._state in ["RUNNING", "PENDING"]:
                self._state = "CANCELLED"
                self.set_exception(OSError(errno.ECANCELED, "canceled"))

    def _run_check(self, client, extract_id: int | str, /):
        check = check_response(client.extract_progress)
        payload = {"extract_id": extract_id}
        def update_progress():
            while self.running():
                try:
                    data = check(payload)["data"]
                    if not data:
                        raise OSError(errno.EINVAL, f"no such extract_id: {extract_id}")
                    progress = data["percent"]
                    self.progress = progress
                    if progress == 100:
                        self.set_result(data)
                        return
                except BaseException as e:
                    self.set_exception(e)
                    return
                sleep(1)
        Thread(target=update_progress).start()


# 上传分几种：1. 不获取 hash 等信息，直接一次上传 2. 获取 hash 等信息，一次上传 3. 获取 hash 等信息，分块上传
# http 的读写都基于 socket，设计一个方案，把上传进行封装为一个文件，可以自行决定一次写入多少，底层会多次调用write，最后flush
# 如果文件足够小，小于 1MB，是不需要 2 次哈希的

