# /// script
# requires-python = ">=3.9"
# dependencies = ["httpx[socks]==0.28.1", "qrcode[pil]==8.2"]
# ///

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
REFERER = "https://www.bilibili.com/"
WBI_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
VIDEO_CODEC_PRIORITY = [7, 12, 13]
COOKIE_KEYS = ["SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3", "buvid4"]
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "bili23-downloader"
COOKIE_FILE = CONFIG_DIR / "cli-cookie.json"
QR_WAITING_FOR_SCAN = 86101
QR_WAITING_FOR_CONFIRMATION = 86090
QR_SUCCESS = 0
QR_EXPIRED = 86038


class CliError(Exception):
    pass


def load_stored_cookie() -> str | None:
    try:
        data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    cookie = data.get("cookie")
    return cookie if isinstance(cookie, str) and cookie else None


def save_stored_cookie(cookie: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookie": cookie,
        "updated_at": int(time.time()),
    }
    COOKIE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        COOKIE_FILE.chmod(0o600)
    except OSError:
        pass


def clear_stored_cookie() -> bool:
    try:
        COOKIE_FILE.unlink()
        return True
    except FileNotFoundError:
        return False


def cookie_string_from_client(client: httpx.Client) -> str:
    cookies: dict[str, str] = {}
    for cookie in client.cookies.jar:
        if cookie.name in COOKIE_KEYS and cookie.value:
            cookies[cookie.name] = cookie.value

    if not cookies.get("SESSDATA"):
        raise CliError("扫码成功，但没有拿到 SESSDATA Cookie")

    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def aid_to_bvid(aid: int) -> str:
    xor_code = 23442827791579
    max_aid = 1 << 51
    alphabet = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
    encode_map = 8, 7, 0, 5, 1, 3, 2, 4, 6

    bvid = [""] * 9
    tmp = (max_aid | aid) ^ xor_code

    for index in encode_map:
        bvid[index] = alphabet[tmp % len(alphabet)]
        tmp //= len(alphabet)

    return "BV1" + "".join(bvid)


def parse_video_url(url: str) -> tuple[str, int | None]:
    bvid = re.search(r"BV\w+", url)
    if bvid:
        parsed = urlparse(url)
        page_values = parse_qs(parsed.query).get("p")
        page = int(page_values[0]) if page_values and page_values[0].isdigit() else None
        return bvid.group(0), page

    aid = re.search(r"av(\d+)", url)
    if aid:
        return aid_to_bvid(int(aid.group(1))), None

    raise CliError("只支持普通投稿视频链接，且链接中需要包含 BV 或 av 号")


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:180] or "bilibili-video"


def get_mixin_key(img_key: str, sub_key: str) -> str:
    origin = img_key + sub_key
    return "".join(origin[index] for index in WBI_MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi(params: dict, img_key: str, sub_key: str) -> str:
    params = dict(params)
    params["wts"] = round(time.time())
    params = dict(sorted(params.items()))
    params = {
        key: "".join(char for char in str(value) if char not in "!'()*")
        for key, value in params.items()
    }
    query = urlencode(params)
    params["w_rid"] = hashlib.md5((query + get_mixin_key(img_key, sub_key)).encode()).hexdigest()
    return urlencode(params)


def request_json(client: httpx.Client, url: str, params: dict | None = None) -> dict:
    response = client.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    code = data.get("code", 0)
    if code != 0:
        message = data.get("message", data)
        if code in (-101, 87008):
            message = f"{message}（需要登录或当前账号没有该视频权限，可运行 bili23 login，或通过 --cookie / BILI23_COOKIE 传入登录态）"
        raise CliError(f"Bilibili API 返回错误: {message}")

    return data


def prepare_client(cookie: str | None, user_agent: str) -> httpx.Client:
    cookie = cookie or os.environ.get("BILI23_COOKIE") or load_stored_cookie()
    headers = {
        "User-Agent": user_agent,
        "Referer": REFERER,
    }
    if cookie:
        headers["Cookie"] = cookie

    client = httpx.Client(headers=headers, follow_redirects=True, timeout=30)

    try:
        request_json(client, "https://api.bilibili.com/x/frontend/finger/spi")
    except Exception:
        pass

    return client


def prepare_login_client(user_agent: str) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent, "Referer": REFERER},
        follow_redirects=True,
        timeout=30,
    )


def get_nav_data(client: httpx.Client) -> dict:
    response = client.get("https://api.bilibili.com/x/web-interface/nav")
    response.raise_for_status()
    return response.json()


def print_login_qrcode(qrcode_url: str) -> None:
    try:
        import qrcode
    except ImportError as error:
        raise CliError("缺少 qrcode 依赖，无法显示扫码登录二维码") from error

    qr = qrcode.QRCode(border=1)
    qr.add_data(qrcode_url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def login(args: argparse.Namespace) -> int:
    with prepare_login_client(args.user_agent) as client:
        params = {
            "source": "main-fe-header",
            "go_url": "https://www.bilibili.com/",
            "web_location": "333.1007",
        }
        data = request_json(
            client,
            f"https://passport.bilibili.com/x/passport-login/web/qrcode/generate?{urlencode(params)}",
        )["data"]
        qrcode_url = data["url"]
        qrcode_key = data["qrcode_key"]

        print("请用 Bilibili 手机客户端扫码登录：")
        print_login_qrcode(qrcode_url)

        deadline = time.monotonic() + args.timeout
        last_status = None
        while time.monotonic() < deadline:
            response = client.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": qrcode_key},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code", 0) != 0:
                raise CliError(payload.get("message", "扫码登录失败"))

            status = payload.get("data", {}).get("code")
            if status == QR_SUCCESS:
                cookie = cookie_string_from_client(client)
                save_stored_cookie(cookie)
                print(f"登录成功，Cookie 已保存到 {COOKIE_FILE}")
                return 0

            if status == QR_EXPIRED:
                raise CliError("二维码已过期，请重新运行 bili23 login")

            if status != last_status:
                if status == QR_WAITING_FOR_SCAN:
                    print("等待扫码...")
                elif status == QR_WAITING_FOR_CONFIRMATION:
                    print("已扫码，等待手机确认...")
                else:
                    print(payload.get("data", {}).get("message", f"等待确认，状态码 {status}"))
                last_status = status

            time.sleep(1)

    raise CliError("登录超时，请重新运行 bili23 login")


def login_status(args: argparse.Namespace) -> int:
    cookie = os.environ.get("BILI23_COOKIE") or load_stored_cookie()
    if not cookie:
        print("未登录：没有找到已保存的 Cookie")
        return 1

    client = prepare_client(None, args.user_agent)
    try:
        data = get_nav_data(client)
    finally:
        client.close()

    user_data = data.get("data", {})
    if data.get("code") == 0 and user_data.get("isLogin"):
        print(f"已登录：{user_data.get('uname', '')} ({user_data.get('mid', '')})")
        return 0

    print(f"未登录或 Cookie 已失效：{data.get('message', data)}")
    return 1


def logout(args: argparse.Namespace) -> int:
    if clear_stored_cookie():
        print(f"已删除登录态：{COOKIE_FILE}")
    else:
        print("没有找到已保存的登录态")
    return 0


def get_wbi_keys(client: httpx.Client) -> tuple[str, str]:
    response = client.get("https://api.bilibili.com/x/web-interface/nav")
    response.raise_for_status()
    data = response.json()
    wbi_img = data.get("data", {}).get("wbi_img", {})
    img_url = wbi_img.get("img_url", "")
    sub_url = wbi_img.get("sub_url", "")

    img_key = Path(urlparse(img_url).path).stem
    sub_key = Path(urlparse(sub_url).path).stem

    if not img_key or not sub_key:
        raise CliError("无法获取 WBI key")

    return img_key, sub_key


def request_wbi_json(client: httpx.Client, url: str, params: dict, wbi_keys: tuple[str, str]) -> dict:
    signed_query = sign_wbi(params, *wbi_keys)
    return request_json(client, f"{url}?{signed_query}")


def get_video_page(client: httpx.Client, bvid: str, page: int | None, wbi_keys: tuple[str, str]) -> tuple[dict, dict]:
    view = request_wbi_json(
        client,
        "https://api.bilibili.com/x/web-interface/wbi/view",
        {"bvid": bvid},
        wbi_keys,
    )["data"]
    pages = view.get("pages", [])

    if not pages:
        raise CliError("视频没有可下载分 P 信息")

    page_number = page or 1
    for entry in pages:
        if entry.get("page") == page_number:
            return view, entry

    raise CliError(f"找不到第 {page_number} P，当前视频共有 {len(pages)} P")


def get_play_info(client: httpx.Client, bvid: str, cid: int, quality: int, wbi_keys: tuple[str, str]) -> dict:
    data = request_wbi_json(
        client,
        "https://api.bilibili.com/x/player/wbi/playurl",
        {
            "bvid": bvid,
            "cid": cid,
            "qn": quality,
            "fnver": 0,
            "fnval": 4048,
            "fourk": 1,
        },
        wbi_keys,
    )
    return data["data"]


def pick_dash_video(videos: list[dict], quality: int, codec: int | None) -> dict:
    if not videos:
        raise CliError("没有可用视频流")

    if quality > 0:
        same_quality = [entry for entry in videos if entry.get("id") == quality]
        if same_quality:
            videos = same_quality

    best_quality = max(entry.get("id", 0) for entry in videos)
    candidates = [entry for entry in videos if entry.get("id") == best_quality]

    if codec is not None:
        for entry in candidates:
            if entry.get("codecid") == codec:
                return entry

    for codec_id in VIDEO_CODEC_PRIORITY:
        for entry in candidates:
            if entry.get("codecid") == codec_id:
                return entry

    return candidates[0]


def pick_dash_audio(audios: list[dict]) -> dict | None:
    if not audios:
        return None

    return max(audios, key=lambda entry: (entry.get("bandwidth") or 0, entry.get("id") or 0))


def media_urls(media: dict) -> list[str]:
    urls: list[str] = []
    for key in ["baseUrl", "base_url", "backupUrl", "backup_url", "url"]:
        value = media.get(key)
        if isinstance(value, str):
            urls.append(value)
        elif isinstance(value, list):
            urls.extend(value)
    return urls


def pick_reachable_url(client: httpx.Client, urls: list[str]) -> str:
    for url in urls:
        try:
            response = client.head(url, headers={"Referer": REFERER}, follow_redirects=True)
            if response.status_code < 400 and "text" not in response.headers.get("Content-Type", ""):
                return url
        except Exception:
            continue

    if urls:
        return urls[0]

    raise CliError("没有可用下载链接")


def download_file(client: httpx.Client, url: str, path: Path) -> None:
    temp_path = path.with_suffix(path.suffix + ".download")
    downloaded = temp_path.stat().st_size if temp_path.exists() else 0
    headers = {"Referer": REFERER}

    if downloaded:
        headers["Range"] = f"bytes={downloaded}-"

    with client.stream("GET", url, headers=headers, follow_redirects=True, timeout=60) as response:
        if response.status_code == 416:
            temp_path.rename(path)
            return

        response.raise_for_status()
        total = response.headers.get("Content-Length")
        total_size = downloaded + int(total) if total and total.isdigit() else 0
        mode = "ab" if downloaded and response.status_code == 206 else "wb"

        if mode == "wb":
            downloaded = 0

        with temp_path.open(mode + "") as file:
            last_print = 0.0
            for chunk in response.iter_bytes(chunk_size=1024 * 256):
                if not chunk:
                    continue

                file.write(chunk)
                downloaded += len(chunk)

                now = time.monotonic()
                if now - last_print >= 0.5:
                    if total_size:
                        percent = downloaded / total_size * 100
                        print(f"\r下载中 {path.name}: {percent:5.1f}%", end="", flush=True)
                    else:
                        print(f"\r下载中 {path.name}: {downloaded / 1024 / 1024:.1f} MiB", end="", flush=True)
                    last_print = now

    print(f"\r下载完成 {path.name}" + " " * 20)
    temp_path.replace(path)


def run_ffmpeg(args: list[str]) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CliError("找不到 ffmpeg，已下载临时音视频文件但无法合并")

    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
    subprocess.run(command, check=True)


def merge_dash(video_path: Path | None, audio_path: Path | None, output_path: Path, keep: bool) -> None:
    if video_path and audio_path:
        run_ffmpeg(["-i", str(video_path), "-i", str(audio_path), "-c", "copy", str(output_path)])
    elif video_path:
        run_ffmpeg(["-i", str(video_path), "-c", "copy", str(output_path)])
    elif audio_path:
        run_ffmpeg(["-i", str(audio_path), "-c", "copy", str(output_path)])
    else:
        raise CliError("没有可合并的文件")

    if not keep:
        for path in [video_path, audio_path]:
            if path and path.exists():
                path.unlink()


def download_dash(client: httpx.Client, play_info: dict, base_name: str, output_dir: Path, args: argparse.Namespace) -> Path:
    dash = play_info["dash"]
    video = None if args.audio_only else pick_dash_video(dash.get("video", []), args.quality, args.codec)
    audio = None if args.video_only else pick_dash_audio(dash.get("audio", []))

    if not video and not audio:
        raise CliError("没有匹配的音视频流")

    video_path = output_dir / f"{base_name}.video.m4s" if video else None
    audio_path = output_dir / f"{base_name}.audio.m4s" if audio else None

    if video and video_path:
        print(f"视频流: qn={video.get('id')} codec={video.get('codecid')} bandwidth={video.get('bandwidth')}")
        download_file(client, pick_reachable_url(client, media_urls(video)), video_path)

    if audio and audio_path:
        print(f"音频流: id={audio.get('id')} bandwidth={audio.get('bandwidth')}")
        download_file(client, pick_reachable_url(client, media_urls(audio)), audio_path)

    if args.no_merge:
        return video_path or audio_path

    suffix = ".m4a" if args.audio_only else ".mp4"
    output_path = output_dir / f"{base_name}{suffix}"
    print("合并中...")
    merge_dash(video_path, audio_path, output_path, args.keep)
    return output_path


def download_durl(client: httpx.Client, play_info: dict, base_name: str, output_dir: Path) -> Path:
    parts = play_info.get("durl", [])
    if not parts:
        raise CliError("没有 durl 下载地址")

    if len(parts) == 1:
        output_path = output_dir / f"{base_name}.mp4"
        download_file(client, pick_reachable_url(client, media_urls(parts[0])), output_path)
        return output_path

    part_paths = []
    for index, part in enumerate(parts, start=1):
        part_path = output_dir / f"{base_name}.part{index}.mp4"
        download_file(client, pick_reachable_url(client, media_urls(part)), part_path)
        part_paths.append(part_path)

    list_path = output_dir / f"{base_name}.concat.txt"
    list_path.write_text("".join(f"file '{path.name}'\n" for path in part_paths), encoding="utf-8")
    output_path = output_dir / f"{base_name}.mp4"
    print("合并分段中...")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(output_path)])

    for path in [*part_paths, list_path]:
        if path.exists():
            path.unlink()

    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bili23 Downloader CLI（最小版，支持普通 BV/av 投稿视频）",
        epilog="登录相关命令：bili23 login、bili23 status、bili23 logout",
    )
    parser.add_argument("url", help="Bilibili 视频链接")
    parser.add_argument("-o", "--output", default="downloads", help="输出目录，默认 downloads")
    parser.add_argument("-p", "--page", type=int, help="下载第几个分 P，默认使用链接中的 p 参数或第 1 P")
    parser.add_argument("-q", "--quality", type=int, default=127, help="请求画质 qn，默认 127；实际画质取决于账号权限")
    parser.add_argument("--codec", type=int, choices=[7, 12, 13], help="指定视频编码：7=AVC, 12=HEVC, 13=AV1")
    parser.add_argument("--cookie", help="手动传入 Cookie 字符串，用于登录态/高画质")
    parser.add_argument("--user-agent", default=USER_AGENT, help="请求 User-Agent")
    parser.add_argument("--audio-only", action="store_true", help="只下载音频")
    parser.add_argument("--video-only", action="store_true", help="只下载视频")
    parser.add_argument("--no-merge", action="store_true", help="不调用 ffmpeg 合并，保留 m4s/分段文件")
    parser.add_argument("--keep", action="store_true", help="合并后保留临时音视频文件")
    return parser


def build_login_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bili23 Downloader CLI 扫码登录")
    parser.add_argument("--timeout", type=int, default=180, help="扫码超时时间，单位秒，默认 180")
    parser.add_argument("--user-agent", default=USER_AGENT, help="请求 User-Agent")
    return parser


def build_status_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查看 Bili23 Downloader CLI 登录状态")
    parser.add_argument("--user-agent", default=USER_AGENT, help="请求 User-Agent")
    return parser


def build_logout_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="删除 Bili23 Downloader CLI 保存的登录态")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    try:
        if argv and argv[0] == "login":
            args = build_login_parser().parse_args(argv[1:])
            return login(args)

        if argv and argv[0] == "status":
            args = build_status_parser().parse_args(argv[1:])
            return login_status(args)

        if argv and argv[0] == "logout":
            args = build_logout_parser().parse_args(argv[1:])
            return logout(args)

    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except (httpx.HTTPError, CliError, OSError, ValueError) as error:
        print(f"操作失败: {error}", file=sys.stderr)
        return 1

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.audio_only and args.video_only:
        parser.error("--audio-only 和 --video-only 不能同时使用")

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        bvid, page_from_url = parse_video_url(args.url)
        page = args.page or page_from_url

        client = prepare_client(args.cookie, args.user_agent)
        try:
            wbi_keys = get_wbi_keys(client)
            view, page_info = get_video_page(client, bvid, page, wbi_keys)
            title = view.get("title", bvid)
            page_number = page_info.get("page", page or 1)
            part = page_info.get("part") or title
            base_name = sanitize_filename(title if len(view.get("pages", [])) == 1 else f"{title} P{page_number} {part}")

            print(f"标题: {title}")
            print(f"分P: P{page_number} {part}")

            play_info = get_play_info(client, bvid, page_info["cid"], args.quality, wbi_keys)

            if "dash" in play_info:
                output_path = download_dash(client, play_info, base_name, output_dir, args)
            else:
                output_path = download_durl(client, play_info, base_name, output_dir)
        finally:
            client.close()

        print(f"输出: {output_path}")
        return 0

    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except subprocess.CalledProcessError as error:
        print(f"ffmpeg 执行失败: {error}", file=sys.stderr)
        return 1
    except (httpx.HTTPError, CliError, OSError, ValueError) as error:
        print(f"下载失败: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
