from __future__ import (absolute_import, print_function, division)
import Cookie
import copy
from email.utils import parsedate_tz, formatdate, mktime_tz
import time

from libmproxy import utils
from netlib import odict, encoding
from netlib.http import status_codes
from netlib.tcp import Address
from netlib.http.semantics import Request, Response, CONTENT_MISSING
from .. import version, stateobject
from .flow import Flow


class MessageMixin(stateobject.StateObject):
    _stateobject_attributes = dict(
        httpversion=tuple,
        headers=odict.ODictCaseless,
        body=str,
        timestamp_start=float,
        timestamp_end=float
    )
    _stateobject_long_attributes = {"body"}

    def get_state(self, short=False):
        ret = super(MessageMixin, self).get_state(short)
        if short:
            if self.body:
                ret["contentLength"] = len(self.body)
            elif self.body == CONTENT_MISSING:
                ret["contentLength"] = None
            else:
                ret["contentLength"] = 0
        return ret

    def get_decoded_content(self):
        """
            Returns the decoded content based on the current Content-Encoding
            header.
            Doesn't change the message iteself or its headers.
        """
        ce = self.headers.get_first("content-encoding")
        if not self.body or ce not in encoding.ENCODINGS:
            return self.body
        return encoding.decode(ce, self.body)

    def decode(self):
        """
            Decodes body based on the current Content-Encoding header, then
            removes the header. If there is no Content-Encoding header, no
            action is taken.

            Returns True if decoding succeeded, False otherwise.
        """
        ce = self.headers.get_first("content-encoding")
        if not self.body or ce not in encoding.ENCODINGS:
            return False
        data = encoding.decode(ce, self.body)
        if data is None:
            return False
        self.body = data
        del self.headers["content-encoding"]
        return True

    def encode(self, e):
        """
            Encodes body with the encoding e, where e is "gzip", "deflate"
            or "identity".
        """
        # FIXME: Error if there's an existing encoding header?
        self.body = encoding.encode(e, self.body)
        self.headers["content-encoding"] = [e]

    def copy(self):
        c = copy.copy(self)
        c.headers = self.headers.copy()
        return c

    def replace(self, pattern, repl, *args, **kwargs):
        """
            Replaces a regular expression pattern with repl in both the headers
            and the body of the message. Encoded body will be decoded
            before replacement, and re-encoded afterwards.

            Returns the number of replacements made.
        """
        with decoded(self):
            self.body, c = utils.safe_subn(
                pattern, repl, self.body, *args, **kwargs
            )
        c += self.headers.replace(pattern, repl, *args, **kwargs)
        return c


class HTTPRequest(MessageMixin, Request):
    """
    An HTTP request.

    Exposes the following attributes:

        method: HTTP method

        scheme: URL scheme (http/https)

        host: Target hostname of the request. This is not neccessarily the
        directy upstream server (which could be another proxy), but it's always
        the target server we want to reach at the end. This attribute is either
        inferred from the request itself (absolute-form, authority-form) or from
        the connection metadata (e.g. the host in reverse proxy mode).

        port: Destination port

        path: Path portion of the URL (not present in authority-form)

        httpversion: HTTP version tuple, e.g. (1,1)

        headers: odict.ODictCaseless object

        content: Content of the request, None, or CONTENT_MISSING if there
        is content associated, but not present. CONTENT_MISSING evaluates
        to False to make checking for the presence of content natural.

        form_in: The request form which mitmproxy has received. The following
        values are possible:

             - relative (GET /index.html, OPTIONS *) (covers origin form and
               asterisk form)
             - absolute (GET http://example.com:80/index.html)
             - authority-form (CONNECT example.com:443)
             Details: http://tools.ietf.org/html/draft-ietf-httpbis-p1-messaging-25#section-5.3

        form_out: The request form which mitmproxy will send out to the
        destination

        timestamp_start: Timestamp indicating when request transmission started

        timestamp_end: Timestamp indicating when request transmission ended
    """

    def __init__(
            self,
            form_in,
            method,
            scheme,
            host,
            port,
            path,
            httpversion,
            headers,
            body,
            timestamp_start=None,
            timestamp_end=None,
            form_out=None,
    ):
        Request.__init__(
            self,
            form_in,
            method,
            scheme,
            host,
            port,
            path,
            httpversion,
            headers,
            body,
            timestamp_start,
            timestamp_end,
        )
        self.form_out = form_out or form_in

        # Have this request's cookies been modified by sticky cookies or auth?
        self.stickycookie = False
        self.stickyauth = False

        # Is this request replayed?
        self.is_replay = False

    _stateobject_attributes = MessageMixin._stateobject_attributes.copy()
    _stateobject_attributes.update(
        form_in=str,
        method=str,
        scheme=str,
        host=str,
        port=int,
        path=str,
        form_out=str,
        is_replay=bool
    )

    @classmethod
    def from_state(cls, state):
        f = cls(
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None)
        f.load_state(state)
        return f

    @classmethod
    def from_protocol(
            self,
            protocol,
            *args,
            **kwargs
    ):
        req = protocol.read_request(*args, **kwargs)
        return self.wrap(req)

    @classmethod
    def wrap(self, request):
        req = HTTPRequest(
            form_in=request.form_in,
            method=request.method,
            scheme=request.scheme,
            host=request.host,
            port=request.port,
            path=request.path,
            httpversion=request.httpversion,
            headers=request.headers,
            body=request.body,
            timestamp_start=request.timestamp_start,
            timestamp_end=request.timestamp_end,
            form_out=(request.form_out if hasattr(request, 'form_out') else None),
        )
        if hasattr(request, 'stream_id'):
            req.stream_id = request.stream_id
        return req

    def __hash__(self):
        return id(self)

    def replace(self, pattern, repl, *args, **kwargs):
        """
            Replaces a regular expression pattern with repl in the headers, the
            request path and the body of the request. Encoded content will be
            decoded before replacement, and re-encoded afterwards.

            Returns the number of replacements made.
        """
        c = MessageMixin.replace(self, pattern, repl, *args, **kwargs)
        self.path, pc = utils.safe_subn(
            pattern, repl, self.path, *args, **kwargs
        )
        c += pc
        return c


class HTTPResponse(MessageMixin, Response):
    """
    An HTTP response.

    Exposes the following attributes:

        httpversion: HTTP version tuple, e.g. (1, 0), (1, 1), or (2, 0)

        status_code: HTTP response status code

        msg: HTTP response message

        headers: ODict Caseless object

        content: Content of the request, None, or CONTENT_MISSING if there
        is content associated, but not present. CONTENT_MISSING evaluates
        to False to make checking for the presence of content natural.

        timestamp_start: Timestamp indicating when request transmission started

        timestamp_end: Timestamp indicating when request transmission ended
    """

    def __init__(
            self,
            httpversion,
            status_code,
            msg,
            headers,
            body,
            timestamp_start=None,
            timestamp_end=None,
    ):
        Response.__init__(
            self,
            httpversion,
            status_code,
            msg,
            headers,
            body,
            timestamp_start=timestamp_start,
            timestamp_end=timestamp_end,
        )

        # Is this request replayed?
        self.is_replay = False
        self.stream = False

    _stateobject_attributes = MessageMixin._stateobject_attributes.copy()
    _stateobject_attributes.update(
        status_code=int,
        msg=str
    )

    @classmethod
    def from_state(cls, state):
        f = cls(None, None, None, None, None)
        f.load_state(state)
        return f

    @classmethod
    def from_protocol(
            self,
            protocol,
            *args,
            **kwargs
    ):
        resp = protocol.read_response(*args, **kwargs)
        return self.wrap(resp)

    @classmethod
    def wrap(self, response):
        resp = HTTPResponse(
            httpversion=response.httpversion,
            status_code=response.status_code,
            msg=response.msg,
            headers=response.headers,
            body=response.body,
            timestamp_start=response.timestamp_start,
            timestamp_end=response.timestamp_end,
        )
        if hasattr(response, 'stream_id'):
            resp.stream_id = response.stream_id
        return resp

    def _refresh_cookie(self, c, delta):
        """
            Takes a cookie string c and a time delta in seconds, and returns
            a refreshed cookie string.
        """
        c = Cookie.SimpleCookie(str(c))
        for i in c.values():
            if "expires" in i:
                d = parsedate_tz(i["expires"])
                if d:
                    d = mktime_tz(d) + delta
                    i["expires"] = formatdate(d)
                else:
                    # This can happen when the expires tag is invalid.
                    # reddit.com sends a an expires tag like this: "Thu, 31 Dec
                    # 2037 23:59:59 GMT", which is valid RFC 1123, but not
                    # strictly correct according to the cookie spec. Browsers
                    # appear to parse this tolerantly - maybe we should too.
                    # For now, we just ignore this.
                    del i["expires"]
        return c.output(header="").strip()

    def refresh(self, now=None):
        """
            This fairly complex and heuristic function refreshes a server
            response for replay.

                - It adjusts date, expires and last-modified headers.
                - It adjusts cookie expiration.
        """
        if not now:
            now = time.time()
        delta = now - self.timestamp_start
        refresh_headers = [
            "date",
            "expires",
            "last-modified",
        ]
        for i in refresh_headers:
            if i in self.headers:
                d = parsedate_tz(self.headers[i][0])
                if d:
                    new = mktime_tz(d) + delta
                    self.headers[i] = [formatdate(new)]
        c = []
        for i in self.headers["set-cookie"]:
            c.append(self._refresh_cookie(i, delta))
        if c:
            self.headers["set-cookie"] = c


class HTTPFlow(Flow):
    """
    A HTTPFlow is a collection of objects representing a single HTTP
    transaction. The main attributes are:

        request: HTTPRequest object
        response: HTTPResponse object
        error: Error object
        server_conn: ServerConnection object
        client_conn: ClientConnection object

    Note that it's possible for a Flow to have both a response and an error
    object. This might happen, for instance, when a response was received
    from the server, but there was an error sending it back to the client.

    The following additional attributes are exposed:

        intercepted: Is this flow currently being intercepted?
        live: Does this flow have a live client connection?
    """

    def __init__(self, client_conn, server_conn, live=None):
        super(HTTPFlow, self).__init__("http", client_conn, server_conn, live)
        self.request = None
        """@type: HTTPRequest"""
        self.response = None
        """@type: HTTPResponse"""

    _stateobject_attributes = Flow._stateobject_attributes.copy()
    _stateobject_attributes.update(
        request=HTTPRequest,
        response=HTTPResponse
    )

    @classmethod
    def from_state(cls, state):
        f = cls(None, None)
        f.load_state(state)
        return f

    def __repr__(self):
        s = "<HTTPFlow"
        for a in ("request", "response", "error", "client_conn", "server_conn"):
            if getattr(self, a, False):
                s += "\r\n  %s = {flow.%s}" % (a, a)
        s += ">"
        return s.format(flow=self)

    def copy(self):
        f = super(HTTPFlow, self).copy()
        if self.request:
            f.request = self.request.copy()
        if self.response:
            f.response = self.response.copy()
        return f

    def match(self, f):
        """
            Match this flow against a compiled filter expression. Returns True
            if matched, False if not.

            If f is a string, it will be compiled as a filter expression. If
            the expression is invalid, ValueError is raised.
        """
        if isinstance(f, basestring):
            from .. import filt

            f = filt.parse(f)
            if not f:
                raise ValueError("Invalid filter expression.")
        if f:
            return f(self)
        return True

    def replace(self, pattern, repl, *args, **kwargs):
        """
            Replaces a regular expression pattern with repl in both request and
            response of the flow. Encoded content will be decoded before
            replacement, and re-encoded afterwards.

            Returns the number of replacements made.
        """
        c = self.request.replace(pattern, repl, *args, **kwargs)
        if self.response:
            c += self.response.replace(pattern, repl, *args, **kwargs)
        return c


class decoded(object):
    """
    A context manager that decodes a request or response, and then
    re-encodes it with the same encoding after execution of the block.

    Example:
    with decoded(request):
        request.content = request.content.replace("foo", "bar")
    """

    def __init__(self, o):
        self.o = o
        ce = o.headers.get_first("content-encoding")
        if ce in encoding.ENCODINGS:
            self.ce = ce
        else:
            self.ce = None

    def __enter__(self):
        if self.ce:
            self.o.decode()

    def __exit__(self, type, value, tb):
        if self.ce:
            self.o.encode(self.ce)


def make_error_response(status_code, message, headers=None):
    response = status_codes.RESPONSES.get(status_code, "Unknown")
    body = """
        <html>
            <head>
                <title>%d %s</title>
            </head>
            <body>%s</body>
        </html>
    """.strip() % (status_code, response, message)

    if not headers:
        headers = odict.ODictCaseless()
    headers["Server"] = [version.NAMEVERSION]
    headers["Connection"] = ["close"]
    headers["Content-Length"] = [len(body)]
    headers["Content-Type"] = ["text/html"]

    return HTTPResponse(
        (1, 1),  # FIXME: Should be a string.
        status_code,
        response,
        headers,
        body,
    )


def make_connect_request(address):
    address = Address.wrap(address)
    return HTTPRequest(
        "authority", "CONNECT", None, address.host, address.port, None, (1, 1),
        odict.ODictCaseless(), ""
    )


def make_connect_response(httpversion):
    headers = odict.ODictCaseless([
        ["Content-Length", "0"],
        ["Proxy-Agent", version.NAMEVERSION]
    ])
    return HTTPResponse(
        httpversion,
        200,
        "Connection established",
        headers,
        "",
    )