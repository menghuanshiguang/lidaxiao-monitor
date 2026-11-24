"""free 网关（本机 dsh-opencode-free）接入的离线测试。

用一个真的 127.0.0.1 假网关（http.server）跑，不联网、不依赖 bun：
  1. 模型 id 来自**请求时刻**的 /v1/models，不写死；只挑 -free 结尾的
  2. 非 -free 的 id 不会被选中
  3. 网关 500 / 不可达 -> 抛错 -> 链路降级到下一个 provider（不回落直连 Zen）
  4. LIDAXIAO_LLM=free 时优先；默认排在主通道之后当 fallback
"""
import contextlib
import io
import json
import os
import sys
import threading
import http.server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from lidaxiao.analyze import llm as L  # noqa: E402

OK = FAIL = 0


def chk(name, cond, info=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     {info}")


class FakeGateway:
    """可编程的假网关: models 列表、chat 状态码、是否记录调用。"""

    def __init__(self, ids=("opencode/grok-code-free", "opencode/paid-model"),
                 chat_status=200, chat_text="ok", detail="FreeTierError"):
        self.ids = list(ids)
        self.chat_status = chat_status
        self.chat_text = chat_text
        self.detail = detail
        self.calls = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, headers=None):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.calls.append(("GET", self.path))
                if self.path == "/v1/models":
                    self._send(200, {"object": "list",
                                     "data": [{"id": i} for i in outer.ids]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                ln = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(ln) or b"{}")
                outer.calls.append(("POST", self.path, body.get("model")))
                if outer.chat_status != 200:
                    self._send(outer.chat_status, {"error": {"message": "boom"}},
                               {"x-free-lane-code": "FreeUsageLimitError"})
                    return
                self._send(200, {"choices": [{"message": {"role": "assistant",
                                                          "content": outer.chat_text}}]},
                           {"x-free-lane-code": outer.detail})

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def close(self):
        self.srv.shutdown()


def _fail(fn):
    try:
        fn()
    except Exception:
        return True
    return False


print("== 模型 id 来自请求时刻的列表 ==")
gw = FakeGateway()
try:
    pcfg = {"provider": "free-gateway", "base_url": gw.base, "model": "",
            "max_tokens": 100, "timeout": 10}
    ids = L.free_gateway_models(gw.base)
    chk("列表里 -free 被挑出来, 非 -free 不在里面",
        ids == ["opencode/grok-code-free"], ids)
    chk("模型空 -> 取第一个 -free", L.free_gateway_model(dict(pcfg)) == "opencode/grok-code-free")
    gw.ids = ["opencode/only-paid-model"]
    chk("列表里没有 -free -> 抛错(该通道不可用)",
        _fail(lambda: L.free_gateway_model(dict(pcfg))), "没抛")
    gw.ids = ["opencode/grok-code-free", "opencode/another-model-free"]
    got = L.free_gateway_model(dict(pcfg))
    chk("多个 -free 取第一个", got == "opencode/grok-code-free", got)
    chk("模型写死时不查列表(尊重配置)",
        L.free_gateway_model({"base_url": gw.base, "model": "opencode/fixed-free"})
        == "opencode/fixed-free")
finally:
    gw.close()


print("== 一次 chat ==")
gw = FakeGateway(chat_text="看多: 降准利好")
try:
    out = L._call_free_gateway({"base_url": gw.base, "model": "", "max_tokens": 50,
                                "timeout": 10}, "系统", "用户")
    chk("返回正文", out == "看多: 降准利好", out)
    post = [c for c in gw.calls if c[0] == "POST"][0]
    chk("请求体 model 用 -free id", post[2] == "opencode/grok-code-free", post)
finally:
    gw.close()

print("== 失败要降级, 且不回落直连 Zen ==")
gw = FakeGateway(chat_status=500)
try:
    chk("网关 500 -> 抛错(交给下一个 provider)",
        _fail(lambda: L._call_free_gateway({"base_url": gw.base, "model": "", "timeout": 10},
                                           "s", "u")), "500 没抛")
finally:
    gw.close()
chk("网关不可达 -> 抛错",
    _fail(lambda: L._call_free_gateway({"base_url": "http://127.0.0.1:1/v1", "model": "",
                                        "timeout": 3}, "s", "u")), "不可达没抛")
chk("provider 分派不再走旧直连车道",
    L.FREE_PROVIDER != "free-gateway" and L.FREE_GATEWAY_PROVIDER == "free-gateway",
    (L.FREE_PROVIDER, L.FREE_GATEWAY_PROVIDER))

print("== 链路顺序 ==")


def chain(env_value):
    """把 _call_llm_once 打桩成只记名字, 看 provider 顺序。"""
    seen = []
    saved_once, saved_key = L._call_llm_once, L.load_llm_key
    cfg = {"llm": {"provider": "opencode-go", "model": "m"},
           "llm_fallback": {"provider": "deepseek", "model": "d"},
           "llm_free": {"enabled": True, "provider": "free-gateway",
                        "base_url": "http://127.0.0.1:9/v1", "model": ""},
           "llm_cli": {"provider": "deepseek-chat-cli"}}
    old_env = os.environ.get("LIDAXIAO_LLM")
    try:
        if env_value is None:
            os.environ.pop("LIDAXIAO_LLM", None)
        else:
            os.environ["LIDAXIAO_LLM"] = env_value
        def _always_fail(pcfg, key, s, u):
            seen.append(pcfg["provider"])
            raise RuntimeError("故意失败: 只为看清尝试顺序")
        L._call_llm_once = _always_fail
        L.load_llm_key = lambda provider=None: "k"
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                L.call_llm(cfg, "k", "s", "u")
        except RuntimeError:
            pass          # 全挂是预期的: 我们要的是"尝试过的顺序"
    finally:
        L._call_llm_once, L.load_llm_key = saved_once, saved_key
        if old_env is None:
            os.environ.pop("LIDAXIAO_LLM", None)
        else:
            os.environ["LIDAXIAO_LLM"] = old_env
    return seen


order_default = chain(None)
chk("默认: free 排在最后(只有主通道失败才轮到它)",
    order_default[0] != "free-gateway" and order_default[-1] == "free-gateway", order_default)
order_force = chain("free")
chk("LIDAXIAO_LLM=free: free 排第一", order_force[0] == "free-gateway", order_force)
chk("开关大小写不敏感", chain("FREE")[0] == "free-gateway")
chk("开关给别的值不影响顺序", chain("main")[-1] == "free-gateway")

print(f"\n{OK} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
