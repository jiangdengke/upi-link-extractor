from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
from time import time

from .credentials import CredentialError, parse_credential, redact_sensitive
from .extractor import ExtractionOptions, extract_upi_link


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="为已授权账号提取 Stripe UPI 支付链接")
    parser.add_argument("--credential-file", type=Path, help="包含 Access Token 或 Session JSON 的文件")
    parser.add_argument("--email", default="", help="Token 中没有邮箱时手动填写")
    parser.add_argument("--proxy-file", type=Path, help="India 代理池文件，每行一个")
    parser.add_argument("--login-proxy", default="", help="登录/Checkout 代理")
    parser.add_argument("--approve-retries", type=int, default=30)
    parser.add_argument("--approve-concurrency", type=int, default=4)
    parser.add_argument("--proxy-from-step", type=int, default=3)
    parser.add_argument("--qr", type=Path, help="二维码输出路径")
    return parser


def _read_credential(path: Path | None) -> str:
    if path:
        return path.read_text(encoding="utf-8").strip()
    from_env = os.getenv("CHATGPT_ACCESS_TOKEN", "").strip()
    if from_env:
        return from_env
    return getpass.getpass("Access Token / JSON: ").strip()


async def _run(args: argparse.Namespace) -> int:
    raw = _read_credential(args.credential_file)
    try:
        credential = parse_credential(raw, args.email)
    except CredentialError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    proxies: tuple[str, ...] = ()
    if args.proxy_file:
        proxies = tuple(
            line.strip()
            for line in args.proxy_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    options = ExtractionOptions(
        proxy_pool=proxies,
        login_proxy=args.login_proxy.strip() or None,
        approve_retries=max(1, min(60, args.approve_retries)),
        approve_concurrency=max(1, min(20, args.approve_concurrency)),
        proxy_from_step=max(1, min(6, args.proxy_from_step)),
    )
    qr_path = args.qr or Path("runtime/qr") / f"cli-{int(time())}.png"

    def log(message: str) -> None:
        print(redact_sensitive(message, credential.access_token), flush=True)

    result = await extract_upi_link(
        credential,
        options,
        qr_path.resolve(),
        log,
        lambda: False,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
