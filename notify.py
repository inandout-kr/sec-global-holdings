"""새 13F 공시 텔레그램 알림.

update.py 가 운용사별 데이터를 만든 뒤 alert() 를 부른다. 운용사마다 최신 분기의
접수번호(원본 + 정정)를 cache/notified.json 에 기억해 두고, 바뀐 곳만 알린다.
새 공시가 없으면 없다고 한 줄 보낸다.

봇 설정은 git에 올리지 않는다: 환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID,
또는 telegram.json {"bot_token": "...", "chat_id": "..."}.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "cache" / "notified.json"
CREDS = ROOT / "telegram.json"
SITE_URL = "https://inandout-kr.github.io/sec-global-holdings/"
TOP_CHANGES = 5   # 비중 증가·감소
TOP_LISTS = 10    # 신규 편입·전량 매도 (넘치면 "외 N개")


def _creds() -> tuple[str, str]:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if (not token or not chat) and CREDS.exists():
        c = json.loads(CREDS.read_text(encoding="utf-8"))
        token, chat = token or c.get("bot_token", ""), chat or str(c.get("chat_id", ""))
    return token, chat


def send(text: str) -> None:
    token, chat = _creds()
    if not token or not chat:
        raise RuntimeError("텔레그램 설정이 없습니다 (telegram.json 또는 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=body, timeout=30) as r:
        if not json.loads(r.read()).get("ok"):
            raise RuntimeError("텔레그램 전송 실패")


def _usd(v: float) -> str:
    a, s = abs(v), "-" if v < 0 else ""
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"{s}${a / div:.{1 if a / div >= 10 else 2}f}{unit}"
    return f"{s}${a:.0f}"


def _pct(p: float) -> str:
    x = p * 100
    return f"{x:+,.0f}%" if abs(x) >= 100 else f"{x:+.1f}%"


def _sym(r: list) -> str:
    return f"${r[1]}" if r[1] else r[2].title()


def _key(q: dict) -> dict:
    return {"main": q["accession"], "amend": q.get("amendments", [])}


def message(fund: dict, amended_only: bool) -> str:
    """운용사 한 곳의 최신 분기 요약. 주식만 (옵션·채권 제외)."""
    q = fund["quarters"][0]
    rows = [r for r in q["rows"] if r[4] == "" and r[5] == "SH"]
    total = q["total"]
    name = fund.get("name_ko") or fund["name"]
    who = f"{name} ({fund['manager']})" if fund.get("manager") else name
    chg = f" ({_pct(total / q['prev_total'] - 1)})" if q.get("prev_total") else ""
    w = lambda v: f"{v / total * 100:.1f}%" if total else ""

    # 비중 증가·감소: 추정 매매액(주식 수 변화 × 분기말 단가)이 큰 순서. 작은 종목의 수천 % 변동이 위를 덮지 않게.
    up = sorted((r for r in rows if r[10] == "U"), key=lambda r: -r[11])[:TOP_CHANGES]
    down = sorted((r for r in rows if r[10] == "D"), key=lambda r: r[11])[:TOP_CHANGES]
    new = sorted((r for r in rows if r[10] == "N"), key=lambda r: -r[6])
    sold = sorted((r for r in rows if r[10] == "S"), key=lambda r: -r[8])

    lines = [
        f"📄 {who}" + (" · 정정 공시" if amended_only else ""),
        f"{q['label']} 13F · 공시 {q['filed']}",
        f"총 평가액 {_usd(total)}{chg} · {q['count']:,}종목",
        "",
        f"▲ 비중 크게 증가 TOP{TOP_CHANGES}",
    ]
    lines += [f"{i}. {_sym(r)} {_pct((r[7] - r[9]) / r[9])} (+{_usd(r[11])}, 비중 {w(r[6])})"
              for i, r in enumerate(up, 1)] or ["없음"]
    lines += ["", f"▼ 비중 크게 감소 TOP{TOP_CHANGES}"]
    lines += [f"{i}. {_sym(r)} {_pct((r[7] - r[9]) / r[9])} ({_usd(r[11])}, 비중 {w(r[6])})"
              for i, r in enumerate(down, 1)] or ["없음"]
    lines += ["", f"🆕 신규 편입 {len(new)}개"]
    lines += [f"{i}. {_sym(r)} {_usd(r[6])} ({w(r[6])})" for i, r in enumerate(new[:TOP_LISTS], 1)] or ["없음"]
    if len(new) > TOP_LISTS:
        lines.append(f"…외 {len(new) - TOP_LISTS}개")
    lines += ["", f"❌ 전량 매도(편출) {len(sold)}개"]
    lines += [f"{i}. {_sym(r)} 직전 {_usd(r[8])}" for i, r in enumerate(sold[:TOP_LISTS], 1)] or ["없음"]
    if len(sold) > TOP_LISTS:
        lines.append(f"…외 {len(sold) - TOP_LISTS}개")
    lines += ["", f"{SITE_URL}#{fund['cik']}"]
    return "\n".join(lines)


def alert(funds: list[dict], log) -> None:
    """새 공시가 있는 운용사마다 한 통씩, 없으면 '없음' 한 통."""
    current = {f["cik"]: _key(f["quarters"][0]) for f in funds}
    if not STATE.exists():  # 처음 켰을 때는 지금 있는 공시를 '이미 알림'으로 두고 다음 공시부터 보낸다
        STATE.write_text(json.dumps(current), encoding="utf-8")
        log("알림: 기준점 저장 (다음 새 공시부터 보냄)")
        return
    notified = json.loads(STATE.read_text(encoding="utf-8"))
    changed = [f for f in funds if notified.get(f["cik"]) != current[f["cik"]]]
    try:
        if not changed:
            send(f"13F 새 공시 없음 ({datetime.now():%m/%d %H:%M} 확인 · 운용사 {len(funds)}곳)")
            log("알림: 새 공시 없음 전송")
            return
        changed.sort(key=lambda f: -f["quarters"][0]["total"])
        send(f"13F 새 공시 {len(changed)}곳 ({datetime.now():%m/%d %H:%M})\n"
             + "\n".join(f"· {f.get('name_ko') or f['name']}" for f in changed))
        for f in changed:
            prev = notified.get(f["cik"]) or {}
            send(message(f, amended_only=prev.get("main") == current[f["cik"]]["main"]))
            notified[f["cik"]] = current[f["cik"]]  # 보낸 곳만 기록: 중간에 실패하면 남은 곳은 다음 실행 때 보낸다
            STATE.write_text(json.dumps(notified), encoding="utf-8")
        log(f"알림: {len(changed)}곳 전송")
    except Exception as e:
        log(f"! 알림 실패: {e}")
