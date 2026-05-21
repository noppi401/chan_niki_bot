import asyncio
import os
import base64
import contextlib
import discord
import subprocess
import sys
from openai import OpenAI
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
import json
import httpx
import ngrok
import re
from pathlib import Path
from typing import Any

load_dotenv(Path(__file__).parent / ".env", override=True)
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_KEY")
client = OpenAI(api_key=OPENAI_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

honesty_level = 70  # デフォルト：友人同士レベル

TRPG_NGROK_URL_FILE = Path(
    os.getenv("TRPG_NGROK_URL_FILE")
    or os.getenv("NGROK_URL_FILE")
    or r"C:\Users\noppi\Documents\Products\trpg_terminal\ngrok-url.txt"
)
AIVENV_API_URL = os.getenv("AIVENV_API_URL", "http://127.0.0.1:8080")
TRPG_PORT = int(os.getenv("TRPG_PORT", "3001"))
AIVENV_LOG_PORT = int(os.getenv("AIVENV_LOG_PORT", "8081"))
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN", "")
NGROK_DOMAIN = os.getenv("NGROK_DOMAIN", "")
AIVENV_DIR = os.getenv("AIVENV_DIR", r"C:\Users\noppi\Documents\Products\vm-play-server")

_aivenv_proc: subprocess.Popen | None = None


def is_aivenv_server_running() -> bool:
    return _aivenv_proc is not None and _aivenv_proc.poll() is None


def start_aivenv_server() -> bool:
    global _aivenv_proc
    if is_aivenv_server_running():
        return False
    aivenv_exe = os.path.join(AIVENV_DIR, ".venv", "Scripts", "aivenv.exe")
    try:
        _aivenv_proc = subprocess.Popen(
            [aivenv_exe, "start"],
            cwd=AIVENV_DIR,
        )
        return True
    except Exception as e:
        print(f"[aivenv start error] {e}")
        return False


def stop_aivenv_server() -> bool:
    global _aivenv_proc
    if not is_aivenv_server_running():
        return False
    try:
        _aivenv_proc.terminate()
        _aivenv_proc = None
        return True
    except Exception as e:
        print(f"[aivenv stop error] {e}")
        return False


# ── Tunnel Manager ────────────────────────────────────────────────────────────

class TunnelManager:
    """ニキ主導でngrokトンネルを切り替える。ポートを変えるたびに既存セッションを閉じて新規接続する。"""

    def __init__(self, auth_token: str, domain: str = "") -> None:
        self._auth_token = auth_token
        self._domain = domain
        self._session: Any = None
        self._listener: Any = None
        self._public_url: str | None = None
        self._current_port: int | None = None

    async def switch_to(self, port: int) -> str | None:
        """既存トンネルを閉じて指定ポートへの新規トンネルを開き、公開URLを返す。"""
        await self._close()
        try:
            builder = ngrok.SessionBuilder()
            if self._auth_token:
                builder = builder.authtoken(self._auth_token)
            else:
                builder = builder.authtoken_from_env()
            session = builder.connect()
            if hasattr(session, "__await__"):
                session = await session
            self._session = session
            ep = session.http_endpoint()
            if self._domain:
                ep = ep.domain(self._domain)
            listener = ep.listen_and_forward(f"http://127.0.0.1:{port}")
            if hasattr(listener, "__await__"):
                listener = await listener
            self._listener = listener
            self._current_port = port
            self._public_url = listener.url()
            return self._public_url
        except Exception as exc:
            print(f"[tunnel switch error] {exc}")
            await self._close()
            return None

    async def _close(self) -> None:
        for obj in (self._listener, self._session):
            if obj is None:
                continue
            with contextlib.suppress(Exception):
                close_fn = getattr(obj, "close", None)
                if close_fn is not None:
                    result = close_fn()
                    if hasattr(result, "__await__"):
                        await result
        self._listener = None
        self._session = None
        self._public_url = None
        self._current_port = None

    @property
    def current_url(self) -> str | None:
        return self._public_url


tunnel_manager = TunnelManager(auth_token=NGROK_AUTHTOKEN, domain=NGROK_DOMAIN)


# ── 釣り機能 ──────────────────────────────────────────────────────────────

def build_fishing_intent_prompt(text: str) -> list:
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    return [
        {
            "role": "system",
            "content": (
                f"今日の日付は {today}（JST）です。\n"
                "あなたは釣りに関する質問を判定するAIです。\n"
                "ユーザーが以下のいずれかを求めている場合は fishing: true にしてください:\n"
                "- 潮汐（大潮・中潮・小潮・干潮・満潮・潮の種類）の質問\n"
                "- 釣りの天気・海況・海水温の質問\n"
                "- 釣りに行けるか・釣れるか・釣り条件の質問\n"
                "- 国府津・東扇島西公園・本牧海づり施設・大黒海づり施設への言及\n\n"
                "対象釣り場が特定できれば location に入れてください（上記4箇所のいずれか）。\n"
                "日付が特定できれば dates に YYYY-MM-DD 形式のリストで入れてください。\n"
                "「今週土日」「来週末」など複数日の場合は複数要素のリストにしてください。\n"
                "JSONのみ返してください:\n"
                "{\"fishing\": true|false, \"location\": \"釣り場名またはnull\", \"dates\": [\"YYYY-MM-DD\", ...]}"
            ),
        },
        {"role": "user", "content": text},
    ]


def is_fishing_question(text: str) -> tuple[bool, str, list[str]]:
    """釣り質問かどうかを判定し (is_fishing, location, dates) を返す"""
    try:
        response = client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=build_fishing_intent_prompt(text),
            max_completion_tokens=120,
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content.strip())
        is_fishing = bool(parsed.get("fishing"))
        location = parsed.get("location") or ""
        dates = parsed.get("dates") or []
        if isinstance(dates, str):
            dates = [dates] if dates else []
        return is_fishing, location, dates
    except Exception as e:
        print(f"[fishing intent parse error] {e}")
        return False, "", []


async def get_fishing_via_mcp(location: str, target_date: str = "") -> str:
    """MCP サーバーから釣り条件テキストを取得する"""
    if not location:
        location = "大黒海づり施設"
    try:
        url = "http://localhost:8000/fishing/conditions"
        params: dict = {"location": location}
        if target_date:
            params["target_date"] = target_date
        async with httpx.AsyncClient() as client_http:
            resp = await client_http.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            return data.get("summary", "")
    except Exception as e:
        print(f"[fishing MCP error] {e}")
        return ""


# ── TRPG / aivenv URL ─────────────────────────────────────────────────────────

def build_ngrok_url_intent_prompt(text: str) -> list:
    return [
        {
            "role": "system",
            "content": (
                "あなたはDiscordメッセージの意図判定器です。"
                "ユーザーがTRPG Terminal、ターミナル、卓、セッション画面、共有画面、ngrokトンネルなどの現在のアクセスURLを求めている場合だけtrueにしてください。"
                "単にURLという語があるだけ、一般的なURL説明、別サービスのURL、Web検索要求、画像URL、参考URLの話はfalseです。"
                "例: 『ターミナルのURL頂戴』『TRPG Terminalのリンク教えて』『卓に入るURLある？』『ngrokのやつ貼って』はtrue。"
                "例: 『このURL見て』『URLとは何？』『DiscordのURL教えて』『参考URLを探して』はfalse。"
                "JSONのみで返してください: {\"terminal_url_request\": true|false}"
            ),
        },
        {"role": "user", "content": text},
    ]


def is_terminal_url_request(text: str) -> bool:
    try:
        response = client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=build_ngrok_url_intent_prompt(text),
            max_completion_tokens=80,
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content.strip())
        return bool(parsed.get("terminal_url_request"))
    except Exception as e:
        print(f"[ngrok url intent parse error] {e}")
        return False


def build_aivenv_url_intent_prompt(text: str) -> list:
    return [
        {
            "role": "system",
            "content": (
                "あなたはDiscordメッセージの意図判定器です。"
                "ユーザーがAI実行環境（aivenv、VM、コード実行、AI実行ログ、実行結果、実行中の画面）のURLやリンクを求めている場合だけtrueにしてください。"
                "TRPG Terminal・卓・セッション画面のURLはfalseです。"
                "例: 『AIの実行ログURL教えて』『VM実行のリンク』『コード動かしてる画面どこ？』はtrue。"
                "例: 『ターミナルのURL』『TRPGのリンク』はfalse。"
                "JSONのみで返してください: {\"aivenv_url_request\": true|false}"
            ),
        },
        {"role": "user", "content": text},
    ]


def is_aivenv_url_request(text: str) -> bool:
    try:
        response = client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=build_aivenv_url_intent_prompt(text),
            max_completion_tokens=80,
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content.strip())
        return bool(parsed.get("aivenv_url_request"))
    except Exception as e:
        print(f"[aivenv url intent parse error] {e}")
        return False


async def get_aivenv_current() -> dict:
    """aivenvの現在の実行状態を取得する。"""
    try:
        async with httpx.AsyncClient() as client_http:
            resp = await client_http.get(f"{AIVENV_API_URL}/current", timeout=5)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        print(f"[aivenv current error] {e}")
        return {"active": False}


def build_aivenv_start_intent_prompt(text: str) -> list:
    return [
        {
            "role": "system",
            "content": (
                "あなたはDiscordメッセージの意図判定器です。"
                "ユーザーがVM・AI実行環境・aivenvのサーバーそのものを『起動』『立ち上げ』することだけを求めている場合にtrueにしてください。"
                "コードの実行・作成・タスク処理の依頼はfalseです（それは別の判定で扱います）。"
                "例: 『VM環境起動して』『aivenv起動して』『AI実行環境立ち上げて』はtrue。"
                "例: 『コード書いて』『スクリプト実行して』『URLは？』はfalse。"
                "JSONのみで返してください: {\"aivenv_start_request\": true|false}"
            ),
        },
        {"role": "user", "content": text},
    ]


def is_aivenv_start_request(text: str) -> bool:
    try:
        response = client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=build_aivenv_start_intent_prompt(text),
            max_completion_tokens=60,
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content.strip())
        return bool(parsed.get("aivenv_start_request"))
    except Exception as e:
        print(f"[aivenv start intent parse error] {e}")
        return False


def build_aivenv_run_intent_prompt(text: str) -> list:
    return [
        {
            "role": "system",
            "content": (
                "あなたはDiscordメッセージの意図判定器です。"
                "ユーザーがAIにコードの実行・作成・タスク処理を依頼している場合だけtrueにしてください。"
                "例: 『ハローワールドを出力するコード書いて』『Pythonでファイル読んで』『このスクリプト実行して』はtrue。"
                "URLを聞いているだけ・ログを見たいだけ・会話・質問のみはfalse。"
                "JSONのみで返してください: {\"aivenv_run_request\": true|false}"
            ),
        },
        {"role": "user", "content": text},
    ]


def is_aivenv_run_request(text: str) -> bool:
    try:
        response = client.chat.completions.create(
            model="gpt-5.4-nano",
            messages=build_aivenv_run_intent_prompt(text),
            max_completion_tokens=60,
            temperature=0,
        )
        parsed = json.loads(response.choices[0].message.content.strip())
        return bool(parsed.get("aivenv_run_request"))
    except Exception as e:
        print(f"[aivenv run intent parse error] {e}")
        return False


async def wait_for_aivenv_ready(timeout_sec: int = 20) -> bool:
    """aivenv API が応答するまで最大 timeout_sec 秒ポーリングする。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    while loop.time() < deadline:
        try:
            async with httpx.AsyncClient() as c:
                resp = await c.get(f"{AIVENV_API_URL}/current", timeout=2)
                if resp.status_code < 500:
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def stop_aivenv_if_running() -> None:
    """実行中のセッションがあれば停止する（409対策）。"""
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"{AIVENV_API_URL}/stop", json={}, timeout=10)
    except Exception:
        pass


async def run_aivenv(instruction: str) -> dict:
    """aivenvの /run エンドポイントにタスクを送信する。"""
    await stop_aivenv_if_running()
    try:
        async with httpx.AsyncClient() as client_http:
            resp = await client_http.post(
                f"{AIVENV_API_URL}/run",
                json={"instruction": instruction},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        print(f"[aivenv run error] {e}")
        return {}


async def get_aivenv_raw_log(execution_id: str) -> str:
    """aivenvログサーバーから生ログを取得する。"""
    try:
        async with httpx.AsyncClient() as client_http:
            resp = await client_http.get(
                f"http://127.0.0.1:{AIVENV_LOG_PORT}/raw/{execution_id}", timeout=5
            )
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        print(f"[aivenv raw log error] {e}")
        return ""


def _infer_execution_success(log_text: str) -> bool | None:
    """ログから成功/失敗を推定する。判定不能な場合はNone。"""
    if not log_text:
        return None
    lower = log_text.lower()
    error_patterns = ["traceback", "error:", "exception:", "exit code 1", "exit code 2", "syntaxerror", "systemerror"]
    for pat in error_patterns:
        if pat in lower:
            return False
    return True


async def wait_for_aivenv_completion(
    execution_id: str,
    channel: discord.abc.Messageable,
    log_url: str,
    timeout_sec: int = 300,
    poll_interval: int = 5,
) -> None:
    """aivenvの実行完了をポーリングし、成否をDiscordに送信する。"""
    elapsed = 0
    while elapsed < timeout_sec:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        try:
            info = await get_aivenv_current()
            if not info.get("active") and elapsed > poll_interval:
                log_text = await get_aivenv_raw_log(execution_id)
                success = _infer_execution_success(log_text)
                if success is False:
                    await channel.send(f"実行完了（エラーあり） ログ: {log_url}")
                else:
                    await channel.send(f"実行完了（成功） ログ: {log_url}")
                return
        except Exception as e:
            print(f"[wait_for_aivenv_completion poll error] {e}")
    await channel.send(f"タイムアウト（{timeout_sec}秒）したよ。ログ確認してみて: {log_url}")


# ── プロンプト ────────────────────────────────────────────────────────────

def get_honesty_prompt(level: int) -> str:
    if level >= 80:
        return (
            f"【正直レベル: {level}%】\n"
            "思ったことをストレートに伝える。遠慮なく指摘・反論もする。\n"
            "ただし人格否定や侮辱はしない。"
        )
    elif level >= 60:
        return (
            f"【正直レベル: {level}%】\n"
            "友人同士のような距離感で、率直かつ思いやりを持って話す。\n"
            "必要なときはハッキリ言うが、相手の気持ちへの配慮も忘れない。"
        )
    elif level >= 40:
        return (
            f"【正直レベル: {level}%】\n"
            "良い面も悪い面も伝えるが、角が立たないよう言葉を選ぶ。\n"
            "批判より改善提案を優先する。"
        )
    elif level >= 20:
        return (
            f"【正直レベル: {level}%】\n"
            "相手の気持ちを最優先にする。批判は最小限に抑え、肯定的な表現を選ぶ。\n"
            "否定的な意見は柔らかく包んで伝える。"
        )
    else:
        return (
            f"【正直レベル: {level}%】\n"
            "ほぼ同意モード。否定的な意見はほぼ出さない。\n"
            "聞こえの良い言葉を選び、相手が心地よく感じる応答を最優先にする。"
        )


@bot.event
async def on_ready():
    print(f"ログイン成功: {bot.user}")


def build_relevance_prompt(question: str, history: list[dict]) -> list:
    return [
        {
            "role": "system",
            "content": (
                """あなたは質問に関連する文脈を抽出し、同時にWeb検索が必要かを判断し、適切な検索キーワードを抽出するAIです。
すでに知っている内容に関してはWeb検索は不要としてください。
以下の履歴は新しい順です。
質問に至るまでに関連する発言を選び、雑談は除外してください。
また、その質問に対してWeb検索を用いるべきかどうかも "web: true/false" で示し、
必要な場合は "keyword" に適切な検索ワードを1つ返してください。
出力形式:
{"context": [{"author": "名前", "content": "発言"}, ...], "web": true, "keyword": "検索用キーワード"}"""
            )
        },
        {
            "role": "user",
            "content": f"""質問: {question}
履歴: {history}"""
        }
    ]


def parse_relevance_response(response_text: str) -> tuple[list[dict], bool, str]:
    try:
        data = json.loads(response_text)
        context = data.get("context", [])
        web_required = data.get("web", False)
        keyword = data.get("keyword", "")
        return context, web_required, keyword
    except Exception as e:
        print(f"[Parse Error] relevance_response: {e}")
        return [], False, ""


async def fetch_image_as_base64(url: str) -> tuple[str, str] | None:
    try:
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(url, timeout=10)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/png").split(";")[0]
            b64 = base64.b64encode(resp.content).decode("utf-8")
            return content_type, b64
    except Exception as e:
        print(f"[画像取得失敗] {e}")
        return None


async def search_via_mcp(query: str) -> list[str]:
    try:
        url = "http://localhost:8000/search"
        params = {"q": query}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", [])
    except Exception as e:
        print(f"[MCP検索失敗] {e}")
        return []


def build_honesty_parse_prompt(text: str) -> list:
    return [
        {
            "role": "system",
            "content": (
                "ユーザーの発言から「正直レベル」の変更意図と数値（0〜100）を読み取り、JSONのみで返してください。\n"
                "変更意図がある場合: {\"change\": true, \"level\": <数値>, \"reason\": \"<解釈理由>\"}\n"
                "変更意図がない（確認・質問など）: {\"change\": false}\n"
                "\n"
                "自然言語の例と対応:\n"
                "・「もっと正直に」「遠慮なく言って」→ 90前後\n"
                "・「友達っぽく」「思いやりを持って」→ 70前後\n"
                "・「優しくして」「柔らかくして」→ 50前後\n"
                "・「毒舌で」「ズバズバ言って」→ 95前後\n"
                "・「褒めてくれるだけでいい」「同意してほしい」→ 10前後\n"
                "数字で直接指定された場合はその値を使う。"
            )
        },
        {
            "role": "user",
            "content": text
        }
    ]


def build_answer_prompt_full(question: str, context: list[dict], web_snippets: list[str], level: int = 70, images: list[tuple[str, str]] = []) -> list:
    context_text = "\n".join([f"[{msg['author']}] {msg['content']}" for msg in context]) if context else ""
    snippet_text = "\n".join(f"- {s}" for s in web_snippets) if web_snippets else "(該当なし)"
    user_text = f"【履歴】\n{context_text}\n\n【Web情報】\n{snippet_text}\n\n【質問】\n{question}"
    if images:
        user_content: str | list = [{"type": "text", "text": user_text}]
        for content_type, b64 in images:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{content_type};base64,{b64}"}
            })
    else:
        user_content = user_text
    return [
        {
            "role": "system",
            "content": (
                "あなたは1998年生まれのネット民アシスタントです。\n"
                "2ch・ニコ動・Twitter・Discordなどのネット文化を通過してきており、\n"
                "ネットミームや定番ネタ、文脈ネタを自然に理解できます。\n"
                "技術情報にも比較的詳しく、実務・実装ベースの視点で話すことができます。\n"
                "また、ユーザー達からあなたは「ニキ」「2chニキ」と呼ばれています。\n"
                "\n"
                "ユーザーとは上下関係のないフラットな立場で、\n"
                "「ネットでよく話す、技術に強い知り合い」程度の距離感を保ちます。\n"
                "口調は砕けすぎず硬すぎず、ネット民らしい簡潔さを重視します。\n"
                "\n"
                "【応答の優先順位】\n"
                "各ルールが競合する場合は以下の順序で優先する。\n"
                "① 【意図推測】で「情報/解決」か「雑談/ミーム」かを判定する（これが全ての基準）\n"
                "② 「情報/解決」と判定した場合：【具体情報の即答優先ルール】→【行動ルール】の順で適用\n"
                "③ 「雑談/ミーム」と判定した場合：【即反射ミームの簡潔返答】→【ネットミーム解釈】→【履歴依存・役割ネタ】の順で適用\n"
                "④ 判定がズレていたと気づいた場合：【誤判定時のフォロー】で軌道修正\n"
                "\n"
                "【意図推測ルール】\n"
                "ユーザーの発言がネタ・雑談目的なのか、情報・解決を求めているのかを\n"
                "文面、語調、質問形式、感情語、履歴などから自動的に推測し、\n"
                "その前提に沿った温度感と情報量で応答する。\n"
                "\n"
                "【具体情報の即答優先ルール】（情報/解決時に最優先）\n"
                "ユーザーが以下のような発言をした場合は、\n"
                "ネタ・雑談文脈であっても、最初に結論となる具体情報を即答する。\n"
                "・「何話」「第何話」「何巻」「どの回」「どのシーン」\n"
                "・「誰」「いつ」「どの作品」「元ネタは何」\n"
                "・番号、固有名詞、特定対象を求める質問\n"
                "即答の後に、補足としてネタとして定着した理由や当時の文脈を簡潔に付け加える。\n"
                "情報 → 小話 の順序は必ず守る。\n"
                "\n"
                "【行動ルール】（情報/解決時に適用）\n"
                "・雑談や感情共有の話題では、まず共感を示す\n"
                "・相談系は否定せず、現実的な選択肢や落とし所を提示する\n"
                "・技術、仕様、実装の質問では実務ベースで説明する\n"
                "・Web検索が必要な場合は、最新情報を踏まえて要点を要約する\n"
                "・一度に情報を詰め込みすぎず、会話の流れを優先する\n"
                "\n"
                "【即反射ミームの簡潔返答】（雑談/ミーム時に最優先）\n"
                "短い定型ミームや呼びかけ（例：「ぬるぽ」「ハーイ、ジョージィ」「STAP細胞は？」など）は、\n"
                "解説や小話を入れず、ネタとしての定番返答のみを即座に返す。\n"
                "通じている感を最優先し、補足説明は不要。一言で終わらせる。\n"
                "\n"
                "【ネットミーム・定番ネタの解釈】（雑談/ミーム時に適用）\n"
                "短文の呼びかけ、有名なフレーズ、固有名詞のみの発言については、\n"
                "事実確認や検索を優先せず、まずネットミーム・定番ネタとして解釈を試みる。\n"
                "ネタとして成立している場合は、正確さよりも「通じている感」を優先する。\n"
                "\n"
                "【履歴依存・役割ネタの対応】（雑談/ミーム時に適用）\n"
                "直前の会話履歴から特定キャラや役割を振られていると判断できる場合は、\n"
                "自分が説明するのではなく、そのキャラの定型セリフや役割として応答する。\n"
                "（例：磯野のセリフ、映画キャラの決まり文句など）\n"
                "\n"
                "【誤判定時のフォロー】\n"
                "直前の応答がユーザーの意図（ネタ／情報）とズレていたと判断できる場合は、\n"
                "キャラ設定に沿った軽い謝りを入れてから話を続ける。\n"
                "言い訳や長い説明はせず、会話の流れを優先する。\n"
                "\n"
                "【口調・雰囲気】\n"
                "・「それな」「普通に」「〜しがち」「分かる」などのネット由来表現を自然に使う\n"
                "・草やwは多用しない（必要な場面で1回程度）\n"
                "・煽り、強い断定、説教口調は避ける\n"
                "・上から目線にならないことを最優先する\n"
                "\n"
                "【表示形式】\n"
                "Discord上で読みやすいよう、不要な空行は入れず、\n"
                "基本は1文〜2文ごとに改行する。\n"
                "\n"
                "【回答ボリューム調整】\n"
                "ユーザーの発言が短い場合は簡潔に返す。\n"
                "詳細を求めていそうな場合のみ段階的に情報量を増やす。\n"
                "\n"
                "【呼びかけ認識ルール】\n"
                "ユーザーが「2chニキ」「ニキ」などと呼びかけた場合、\n"
                "それは自分自身への呼称として認識し、自然に反応する。\n"
                "\n"
                "【禁止事項】\n"
                "・過度な2ch語や猛虎弁\n"
                "・キャラ優先で情報精度を落とすこと\n"
                "・マウント、説教、見下し表現\n"
                "\n"
                "基本スタンスは\n"
                "「ネットの会話として自然で、情報としても信用できる返し」\n"
                "を常に優先すること。\n"
                "\n"
                + get_honesty_prompt(level)
            )
        },
        {
            "role": "user",
            "content": user_content
        }
    ]


@bot.event
async def on_message(message: discord.Message):
    global honesty_level

    if message.author == bot.user:
        return

    # !aivenv コマンド（メンション不要）
    aivenv_match = re.match(r"^!aivenv(?:\s+(\w+))?", message.content.strip())
    if aivenv_match:
        subcmd = (aivenv_match.group(1) or "status").lower()
        if subcmd == "start":
            if is_aivenv_server_running() or await wait_for_aivenv_ready(timeout_sec=2):
                await message.channel.send("aivenv はもう起動してるよ")
            elif start_aivenv_server():
                await message.channel.send("aivenv を起動したよ。準備できるまで少し待ってね。")
            else:
                await message.channel.send("aivenv の起動に失敗したよ。")
        elif subcmd == "stop":
            if stop_aivenv_server():
                await message.channel.send("aivenv を停止したよ。")
            else:
                await message.channel.send("aivenv は起動していないよ。")
        elif subcmd == "status":
            if is_aivenv_server_running():
                await message.channel.send("aivenv: 起動中")
            else:
                await message.channel.send("aivenv: 停止中")
        else:
            await message.channel.send("使い方: `!aivenv start` / `!aivenv stop` / `!aivenv status`")
        return

    # !honesty コマンド（メンション不要）
    honesty_match = re.match(r"^!honesty(?:\s+(\d+))?", message.content.strip())
    if honesty_match:
        if honesty_match.group(1) is None:
            await message.channel.send(f"現在の正直レベル: **{honesty_level}%**")
        else:
            new_level = int(honesty_match.group(1))
            if 0 <= new_level <= 100:
                honesty_level = new_level
                await message.channel.send(f"正直レベルを **{honesty_level}%** に変更したよ")
            else:
                await message.channel.send("0〜100 の範囲で指定してね")
        return

    if not bot.user.mentioned_in(message):
        return

    question = re.sub(r"<@!?[0-9]+>", "", message.content).strip()

    if is_terminal_url_request(question):
        async with message.channel.typing():
            url = await tunnel_manager.switch_to(TRPG_PORT)
        if url:
            TRPG_NGROK_URL_FILE.write_text(url, encoding="ascii")
            await message.channel.send(f"TRPG Terminal: {url}")
        else:
            await message.channel.send("トンネルの切り替えに失敗したよ。ngrokの設定を確認して。")
        return

    if is_aivenv_url_request(question):
        info = await get_aivenv_current()
        if not info.get("active"):
            await message.channel.send("現在実行中のAIタスクはないよ。")
            return
        eid = info.get("execution_id", "")
        async with message.channel.typing():
            url = await tunnel_manager.switch_to(AIVENV_LOG_PORT)
        if url:
            result_url = f"{url.rstrip('/')}/?id={eid}"
            await message.channel.send(f"AI実行ログ: {result_url}")
        else:
            await message.channel.send("トンネルの切り替えに失敗したよ。")
        return

    if is_aivenv_start_request(question):
        if is_aivenv_server_running() or await wait_for_aivenv_ready(timeout_sec=2):
            await message.channel.send("aivenv はもう起動してるよ。")
        elif start_aivenv_server():
            await message.channel.send("aivenv を起動したよ。準備できるまで少し待ってね。")
        else:
            await message.channel.send("aivenv の起動に失敗したよ。")
        return

    if is_aivenv_run_request(question):
        await message.channel.send("了解、実行するよ")
        run_result = await run_aivenv(question)
        if not run_result:
            await message.channel.send("aivenv への接続に失敗したよ。")
            return
        eid = run_result.get("execution_id", "")
        async with message.channel.typing():
            tunnel_url = await tunnel_manager.switch_to(AIVENV_LOG_PORT)
        if tunnel_url:
            log_url = f"{tunnel_url.rstrip('/')}/?id={eid}" if eid else tunnel_url
            await message.channel.send(f"実行開始: {log_url}")
        else:
            await message.channel.send("実行開始したけど、トンネルの切り替えに失敗したよ。")
        return

    # 釣り質問の処理
    fishing, fishing_location, fishing_dates = is_fishing_question(question)
    if fishing:
        if fishing_dates:
            snippets = []
            for d in fishing_dates:
                s = await get_fishing_via_mcp(fishing_location, d)
                if s:
                    snippets.append(s)
            fishing_info = "\n\n".join(snippets)
        else:
            fishing_info = await get_fishing_via_mcp(fishing_location)
        if fishing_info:
            answer_prompt = build_answer_prompt_full(question, [], [fishing_info], honesty_level)
            answer_response = client.chat.completions.create(
                model="gpt-5.4-mini",
                messages=answer_prompt,
                max_completion_tokens=1000,
                temperature=0.5,
            )
            await message.channel.send(answer_response.choices[0].message.content.strip())
            return

    # 自然言語による正直レベル変更
    if "正直レベル" in question:
        try:
            parse_response = client.chat.completions.create(
                model="gpt-5.4-nano",
                messages=build_honesty_parse_prompt(question),
                max_completion_tokens=100,
                temperature=0.1
            )
            parsed = json.loads(parse_response.choices[0].message.content.strip())
            if parsed.get("change"):
                new_level = max(0, min(100, int(parsed["level"])))
                honesty_level = new_level
                reason = parsed.get("reason", "")
                await message.channel.send(
                    f"正直レベルを **{honesty_level}%** に変更したよ"
                    + (f"（{reason}）" if reason else "")
                )
            else:
                await message.channel.send(f"現在の正直レベル: **{honesty_level}%**")
        except Exception as e:
            await message.channel.send(f"正直レベルの解析に失敗した: {e}")
        return

    image_attachments = [a for a in message.attachments if a.content_type and a.content_type.startswith("image/")]
    if not question and not image_attachments:
        return

    if not question and image_attachments:
        question = "この画像を見て、内容を解釈して返答してください。"

    async with message.channel.typing():
        try:
            five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
            history = []

            async for msg in message.channel.history(limit=50):
                if msg.created_at < five_min_ago:
                    break
                if msg.author == bot.user:
                    continue
                history.append({
                    "author": msg.author.display_name,
                    "content": msg.content,
                    "timestamp": msg.created_at.isoformat()
                })
                if len(history) >= 10:
                    break
            history.reverse()

            images = [r for a in image_attachments if (r := await fetch_image_as_base64(a.url)) is not None]

            if images:
                context, web_required, keyword = [], False, ""
            else:
                relevance_prompt = build_relevance_prompt(question, history)
                relevance_response = client.chat.completions.create(
                    model="gpt-5.4-nano",
                    messages=relevance_prompt,
                    max_completion_tokens=1000,
                    temperature=0.2
                )
                context, web_required, keyword = parse_relevance_response(
                    relevance_response.choices[0].message.content.strip()
                )

            web_snippets = await search_via_mcp(keyword) if web_required and keyword else []
            answer_prompt = build_answer_prompt_full(question, context, web_snippets, honesty_level, images)

            answer_response = client.chat.completions.create(
                model="gpt-5.4-mini",
                messages=answer_prompt,
                max_completion_tokens=1000,
                temperature=0.5
            )
            reply = answer_response.choices[0].message.content.strip()
            await message.channel.send(reply)

        except Exception as e:
            await message.channel.send(f"エラーが発生しました: {e}")

bot.run(DISCORD_TOKEN)
