#!/usr/bin/env python3
"""13F 공시 대시보드 자동 생성기.

SEC EDGAR에서 config.json에 등록된 운용사의 13F-HR 공시를 받아와
분기 간 보유 변동(신규 편입 / 확대 / 축소 / 전량 매도)을 계산하고
index.html, data/, output/summary_*.txt 를 만든다.

SEC는 자동 요청에 연락처를 요구한다. 환경변수 SEC_USER_AGENT 또는
sec_user_agent.txt (git에 올리지 않음)에 "이름 이메일" 형식으로 넣는다.

사용법:
    python update.py                # 새 공시 확인 + index.html, data/ 재생성
    python update.py --all-tickers  # 남은 CUSIP 티커 매핑을 전부 수행 (느림)
    python update.py --no-tickers   # 티커 조회 생략 (캐시만 사용)
    python update.py --refetch      # 공시 캐시 무시하고 다시 받기
    python update.py --publish      # 생성 후 내용이 바뀌었으면 GitHub Pages(gh-pages)에 게시
    python update.py --no-backtest  # 따라하기 백테스트(backtest.py) 생략
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
OUT = ROOT / "output"
TEMPLATE = ROOT / "template.html"
DASHBOARD = ROOT / "index.html"
DATA_DIR = ROOT / "data"  # 운용사별 fund_<cik>.js, 분기별 period_<기준일>.js
MAX_PERIODS = 12           # 종목별 화면에 싣는 분기 수 (3년)

# 2023-01-03 이전 접수분은 평가액 단위가 천 달러였다.
VALUE_IN_THOUSANDS_BEFORE = "2023-01-03"
# 그 뒤에도 천 달러 단위로 내는 운용사가 있어 접수분마다 주당 평가액으로 다시 확인한다 (fix_value_unit).
THOUSANDS_MAX_PER_SHARE = 1.5
THOUSANDS_MIN_ROWS = 5
TICKER_RETRY_DAYS = 30


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- HTTP

_last_call = {"sec": 0.0}


def sec_get(url: str, user_agent: str, retries: int = 4) -> bytes:
    """SEC 요청. 초당 10회 제한을 넉넉히 지키고 일시 오류는 재시도한다."""
    for attempt in range(retries):
        wait = 0.2 - (time.time() - _last_call["sec"])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            _last_call["sec"] = time.time()
            return data
        except urllib.error.HTTPError as e:
            _last_call["sec"] = time.time()
            if e.code == 403:
                raise SystemExit(
                    "SEC가 요청을 거부했습니다(403). config.json의 sec_user_agent에 "
                    "'이름 이메일' 형식의 연락처가 들어 있는지 확인하세요."
                ) from e
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- EDGAR

def list_13f_filings(cik: str, ua: str, want: int) -> tuple[list[dict], str]:
    """운용사의 13F-HR / 13F-HR/A 목록 (최신순)과 SEC 등록명.

    은행처럼 다른 공시가 많은 곳은 최근 1,000건 안에 13F가 몇 개 없어서,
    want개 분기가 찰 때까지 이전 페이지를 더 읽는다.
    """
    cik10 = f"{int(cik):010d}"
    sub = json.loads(sec_get(f"https://data.sec.gov/submissions/CIK{cik10}.json", ua))
    out: list[dict] = []

    def collect(block: dict) -> None:
        for form, filed, period, acc in zip(
            block["form"], block["filingDate"], block["reportDate"], block["accessionNumber"]
        ):
            if form in ("13F-HR", "13F-HR/A") and period:
                out.append({"form": form, "filed": filed, "period": period, "accession": acc})

    collect(sub["filings"]["recent"])
    for page in sub["filings"].get("files", [])[:30]:
        if len({f["period"] for f in out if f["form"] == "13F-HR"}) >= want:
            break
        collect(json.loads(sec_get(f"https://data.sec.gov/submissions/{page['name']}", ua)))
    out.sort(key=lambda f: (f["period"], f["filed"]), reverse=True)
    return out, sub.get("name", "")


def pick_quarters(filings: list[dict]) -> list[dict]:
    """분기별로 원본 13F-HR과 그 정정 공시(13F-HR/A)를 묶는다 (최신 분기순)."""
    by_period: dict[str, dict] = {}
    for f in filings:
        slot = by_period.setdefault(f["period"], {"period": f["period"], "main": None, "amendments": []})
        if f["form"] == "13F-HR":
            if slot["main"] is None or f["filed"] > slot["main"]["filed"]:
                slot["main"] = f
        else:
            slot["amendments"].append(f)
    quarters = [q for q in by_period.values() if q["main"]]
    for q in quarters:
        q["amendments"].sort(key=lambda f: f["filed"])
    quarters.sort(key=lambda q: q["period"], reverse=True)
    return quarters


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_info_table(xml_bytes: bytes, scale: int) -> list[list]:
    """정보 테이블 XML → (CUSIP, 옵션구분, 수량유형)별로 합산한 보유 목록."""
    root = ET.fromstring(xml_bytes)
    agg: dict[tuple, list] = {}
    for node in root.iter():
        if _local(node.tag) != "infoTable":
            continue
        d = {_local(el.tag): (el.text or "").strip() for el in node.iter()}
        cusip = d.get("cusip", "").upper()
        if not cusip:
            continue
        pc = d.get("putCall", "").upper()
        typ = d.get("sshPrnamtType", "SH").upper()
        value = int(float(d.get("value") or 0)) * scale
        shares = int(float(d.get("sshPrnamt") or 0))
        key = (cusip, pc, typ)
        if key in agg:
            agg[key][5] += value
            agg[key][6] += shares
        else:
            agg[key] = [cusip, d.get("nameOfIssuer", ""), d.get("titleOfClass", ""), pc, typ, value, shares]
    return list(agg.values())


def fix_value_unit(data: dict) -> dict:
    """2023년 이후에도 평가액을 천 달러 단위로 적어 내는 접수분을 달러로 맞춘다.

    주식 행의 주당 평가액(평가액 ÷ 주식 수) 중앙값이 1.5달러 미만이면 천 달러 단위로 본다.
    행이 몇 개뿐이면 동전주·워런트만 담은 정상 접수분과 구별되지 않으므로 건드리지 않는다.
    캐시에는 원래 값이 남고 읽을 때마다 판정한다 (보정된 값은 다시 걸리지 않는다).
    """
    per_share = [h[5] / h[6] for h in data["holdings"] if h[3] == "" and h[4] == "SH" and h[5] > 0 and h[6] > 0]
    if len(per_share) >= THOUSANDS_MIN_ROWS and statistics.median(per_share) < THOUSANDS_MAX_PER_SHARE:
        for h in data["holdings"]:
            h[5] *= 1000
        if data.get("reported_total"):
            data["reported_total"] *= 1000
    return data


def load_filing(cik: str, filing: dict, ua: str, refetch: bool = False) -> dict:
    """공시 한 건의 보유 목록. cache/<cik>/<accession>.json 에 저장해 재사용한다."""
    path = CACHE / str(int(cik)) / f"{filing['accession']}.json"
    if path.exists() and not refetch:
        return fix_value_unit(json.loads(path.read_text(encoding="utf-8")))

    acc_nodash = filing["accession"].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}"
    index = json.loads(sec_get(f"{base}/index.json", ua))
    xmls = [
        it for it in index["directory"]["item"]
        if it["name"].lower().endswith(".xml") and it["name"].lower() != "primary_doc.xml"
    ]
    if not xmls:
        raise RuntimeError(f"{filing['accession']}: 정보 테이블 XML을 찾지 못했습니다")
    xmls.sort(key=lambda it: int(it.get("size") or 0), reverse=True)

    scale = 1000 if filing["filed"] < VALUE_IN_THOUSANDS_BEFORE else 1
    holdings = parse_info_table(sec_get(f"{base}/{xmls[0]['name']}", ua), scale)

    reported_total, amend_type = None, ""
    try:
        cover = ET.fromstring(sec_get(f"{base}/primary_doc.xml", ua))
        for el in cover.iter():
            tag = _local(el.tag)
            if tag == "tableValueTotal" and el.text:
                reported_total = int(float(el.text)) * scale
            elif tag == "amendmentType" and el.text:
                amend_type = el.text.strip().upper()
    except Exception:  # 표지는 검증·정정 유형 확인용이라 실패해도 계속한다
        pass

    data = {
        "accession": filing["accession"],
        "period": filing["period"],
        "filed": filing["filed"],
        "url": f"{base}/",
        "reported_total": reported_total,
        "amend_type": amend_type,
        "holdings": holdings,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return fix_value_unit(data)


def load_quarter(cik: str, q: dict, ua: str, refetch: bool = False) -> dict | None:
    """원본 13F-HR에 정정 공시를 접수 순서대로 반영한 분기 보유 목록.

    - RESTATEMENT(전체 재작성)이거나 정정분이 사실상 전체 표면 통째로 교체한다.
      (비공개 처리로 원본에 빈 표만 내고 나중에 전체를 내는 운용사가 있다.)
    - NEW HOLDINGS(추가 보유)는 원본에 더한다.
    보유 내용이 아직 공개되지 않은 분기는 None.
    """
    data = load_filing(cik, q["main"], ua, refetch)
    merged = {(h[0], h[3], h[4]): list(h) for h in data["holdings"]}
    reported = data.get("reported_total") or 0
    for a in q["amendments"]:
        amend = load_filing(cik, a, ua, refetch)
        amend_sum = sum(h[5] for h in amend["holdings"])
        current = sum(h[5] for h in merged.values())
        is_full = amend_sum >= 0.9 * max(reported, current, 1)
        if amend.get("amend_type", "").startswith("RESTATE") or is_full:
            merged = {}
        for h in amend["holdings"]:
            key = (h[0], h[3], h[4])
            if key in merged:
                merged[key][5] += h[5]
                merged[key][6] += h[6]
            else:
                merged[key] = list(h)
        reported = amend.get("reported_total") or reported
    holdings = [h for h in merged.values() if h[5] > 0 or h[6] > 0]
    if not holdings:
        return None
    return {
        "accession": data["accession"],
        "period": data["period"],
        "filed": data["filed"],
        "url": data["url"],
        "amendments": [a["filed"] for a in q["amendments"]],
        "holdings": holdings,
    }


# ---------------------------------------------------------------- 분기 비교

def _name_prefix(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())[:5]


def collect_share_ratios(cur: dict, prev: dict, stats: dict) -> None:
    """액면분할과 CUSIP 변경을 가려내기 위한 통계를 운용사마다 모은다 (주식만).

    stats["same"][(분기, cusip)]   = [두 분기 모두 보유한 운용사 수, [주식 수 비율이 크게 튄 값들], 주당 단가 비율]
    stats["moved"][(분기, 옛, 새)] = 옛 CUSIP이 사라지고 이름이 같은 새 CUSIP이 생긴 곳들의 주식 수 비율
    stats["sold"][(분기, 옛)]      = 옛 CUSIP이 통째로 사라진 운용사 수
    """
    period = cur["period"]
    cur_eq = {h[0]: h for h in cur["holdings"] if h[3] == "" and h[4] == "SH"}
    prev_eq = {h[0]: h for h in prev["holdings"] if h[3] == "" and h[4] == "SH"}
    new_by_prefix: dict[str, list] = {}
    sold_by_prefix: dict[str, list] = {}
    for c, h in cur_eq.items():
        p = prev_eq.get(c)
        if p is None:
            new_by_prefix.setdefault(_name_prefix(h[1]), []).append(h)
        elif p[6] > 0 and h[6] > 0:
            # 주당 단가 변화(분기말 가격 비율)는 어느 운용사로 계산해도 같다.
            s = stats["same"].setdefault((period, c), [0, [], (h[5] / h[6]) / (p[5] / p[6]) if p[5] else 0])
            s[0] += 1
            ratio = h[6] / p[6]
            if ratio > 1.4 or ratio < 0.7:
                s[1].append(ratio)
    for c, h in prev_eq.items():
        if c not in cur_eq and h[6] > 0:
            stats["sold"][(period, c)] = stats["sold"].get((period, c), 0) + 1
            sold_by_prefix.setdefault(_name_prefix(h[1]), []).append(h)
    norm = lambda h: re.sub(r"[^A-Z0-9]", "", h[1].upper())
    for prefix, sold in sold_by_prefix.items():
        new = [n for n in new_by_prefix.get(prefix, ()) if n[6] > 0]
        for old in sold:
            # 짝 고르기: 일대일이면 그대로, 여럿이면 이름이 완전히 같은 것, 그래도 여럿이면 주식 수가 거의 같은 것.
            # 끝까지 하나로 좁혀지지 않으면(ETF 신탁처럼 같은 이름이 많은 경우) 건너뛴다.
            picks = new if len(sold) == 1 else []
            if len(picks) != 1:
                picks = [n for n in new if norm(n) == norm(old)]
            if len(picks) != 1:
                picks = [n for n in new if 0.8 <= n[6] / old[6] <= 1.25]
            if len(picks) == 1:
                stats["moved"].setdefault((period, old[0], picks[0][0]), []).append(picks[0][6] / old[6])


def _consensus(ratios: list[float]) -> tuple[float, int]:
    """비율들의 중앙값과, 그 중앙값의 ±3% 안에 드는 개수."""
    med = statistics.median(ratios)
    return med, sum(1 for r in ratios if abs(r / med - 1) <= 0.03)


def resolve_corporate_actions(stats: dict) -> tuple[dict, dict]:
    """매매가 아닌 주식 수 변화를 찾아낸다. (승계, 분할)을 돌려준다.

    승계 {옛 CUSIP: (새 CUSIP, 주식 수 배율)}: 한 분기에 옛 CUSIP을 정리한 운용사의 절반 이상이
        같은 새 CUSIP으로 옮겼고, 두 곳 이상(병합처럼 배율이 1이 아니면 세 곳 이상)의
        주식 수 비율이 일치하면 같은 종목으로 본다.
    분할 {(분기, cusip): 배율}: 계속 보유한 운용사의 절반 이상이 같은 정수배(또는 그 역수) 근처로
        주식 수가 변했고(두 곳 이상은 정확히), 주당 단가도 그만큼 반대로 움직였으면 액면분할·병합으로 본다.
    """
    best: dict[str, tuple] = {}
    for (period, old, new), ratios in stats["moved"].items():
        med, agree = _consensus(ratios)
        factor = 1.0 if 0.8 <= med <= 1.25 else med
        if (agree >= (2 if factor == 1.0 else 3) and len(ratios) >= 0.5 * stats["sold"][(period, old)]
                and len(ratios) > best.get(old, (0,))[0]):
            best[old] = (len(ratios), new, factor)
    links = {old: (new, factor) for old, (_, new, factor) in best.items()}

    multiples = (2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 50)
    candidates = multiples + tuple(1 / m for m in multiples)
    splits = {}
    for key, (holders, ratios, price_ratio) in stats["same"].items():
        if len(ratios) < 2:
            continue
        exact, k = max((sum(1 for r in ratios if abs(r / c - 1) <= 0.03), c) for c in candidates)
        nearby = sum(1 for r in ratios if k / 1.5 <= r <= k * 1.5)  # 분할 뒤 조금 사고판 곳까지 포함
        # 분할이면 (단가 비율 × 배율)이 그 분기의 실제 주가 등락률이어야 한다. 분할이 아닌데 여러 곳이
        # 우연히 같은 배수로 사고판 경우엔 이 값이 배율만큼 어긋나므로, 배율이 클수록 허용 폭을 넓힌다.
        m = max(k, 1 / k)
        if exact >= 2 and nearby >= 0.5 * holders and min(0.6, m ** -0.5) <= price_ratio * k <= max(1.7, m ** 0.5):
            splits[key] = k
    return links, splits


def compare(cur: dict, prev: dict, preds: dict | None = None, splits: dict | None = None) -> list[list]:
    """현재 분기 vs 직전 분기.

    반환 행: [cusip, ticker(빈칸), 종목명, 클래스, 옵션, 수량유형,
             평가액, 수량, 직전 평가액, 직전 수량, 상태, 추정 매매액]
    상태: N 신규 / U 확대 / D 축소 / H 유지 / S 전량 매도
    추정 매매액 = 수량 변화 × 분기말 단가 (매도는 음수).
    preds: 새 CUSIP → [(옛 CUSIP, 배율)]. 직전 분기에 옛 CUSIP으로 들고 있었으면 이어서 비교한다.
    splits: (분기, cusip) → 배율. 직전 수량에 배율을 곱해 분할 전후를 같은 단위로 맞춘다.
    """
    prev_map = {(h[0], h[3], h[4]): h for h in prev["holdings"]}
    rows: list[list] = []
    seen = set()
    for cusip, name, cls, pc, typ, value, shares in cur["holdings"]:
        key = (cusip, pc, typ)
        seen.add(key)
        p = prev_map.get(key)
        factor = 1.0
        if p is not None:
            if splits:
                factor = splits.get((cur["period"], cusip), 1.0)
        elif preds:
            for old, f in preds.get(cusip, ()):
                p = prev_map.get((old, pc, typ))
                if p:
                    seen.add((old, pc, typ))
                    factor = f
                    break
        pv, psh = (p[5], p[6]) if p else (0, 0)
        if factor != 1.0:
            psh = round(psh * factor)
            if abs(shares - psh) <= max(1, psh * 0.001):  # 배율 반올림 오차는 변동으로 보지 않는다
                psh = shares
        if not p or psh <= 0:
            status, trade = "N", value
        elif shares > psh:
            status, trade = "U", round(value * (shares - psh) / shares)
        elif shares < psh:
            status = "D"
            trade = -round(value * (psh - shares) / shares) if shares else -pv
        else:
            status, trade = "H", 0
        rows.append([cusip, "", name, cls, pc, typ, value, shares, pv, psh, status, trade])
    for key, p in prev_map.items():
        if key not in seen:
            rows.append([p[0], "", p[1], p[2], p[3], p[4], 0, 0, p[5], p[6], "S", -p[5]])
    rows.sort(key=lambda r: (r[6], r[8]), reverse=True)
    return rows


# ---------------------------------------------------------------- 티커 매핑 (OpenFIGI)

def _figi_post(jobs: list[dict], api_key: str) -> list[dict]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
    body = json.dumps(jobs).encode()
    for attempt in range(5):
        req = urllib.request.Request("https://api.openfigi.com/v3/mapping", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(20)
                continue
            if e.code >= 500 and attempt < 4:
                time.sleep(5)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 4:
                time.sleep(5)
                continue
            raise
    raise RuntimeError("OpenFIGI 요청 한도 초과가 계속됩니다")


def lookup_tickers(cusips: list[str], cache: dict, api_key: str) -> None:
    """CUSIP → 미국 상장 티커. 결과(없음 포함)는 cache에 기록한다."""
    batch = 100 if api_key else 10
    interval = 0.3 if api_key else 2.5  # 키 없이 분당 25회 제한
    today = date.today().isoformat()
    cache_path = CACHE / "tickers.json"
    total = len(cusips)
    for i in range(0, total, batch):
        chunk = cusips[i:i + batch]
        started = time.time()
        try:
            # 영문자로 시작하는 CUSIP은 해외 설립 법인용 CINS 코드다 (예: Seagate G7997R103).
            res = _figi_post(
                [{"idType": "ID_CINS" if c[0].isalpha() else "ID_CUSIP", "idValue": c, "exchCode": "US"}
                 for c in chunk],
                api_key,
            )
        except Exception as e:  # 티커는 부가 정보 — 실패해도 대시보드는 만든다
            log(f"  ! 티커 조회 중단: {e}")
            break
        for c, r in zip(chunk, res):
            data = r.get("data") or []
            cache[c] = {"t": data[0].get("ticker", "") if data else "", "d": today}
        tmp = cache_path.with_suffix(".tmp")  # 다른 실행이 읽는 도중 반쯤 쓰인 파일을 보지 않게
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache_path)
        done = min(i + batch, total)
        if done % 100 == 0 or done == total:
            log(f"  티커 매핑 {done}/{total}")
        if done < total:
            time.sleep(max(0, interval - (time.time() - started)))


# ---------------------------------------------------------------- 요약 텍스트

def quarter_label(period: str) -> str:
    y, m, _ = period.split("-")
    return f"{y}년 {(int(m) + 2) // 3}분기"


def short_label(period: str) -> str:
    y, m, _ = period.split("-")
    return f"{y[2:]}Q{(int(m) + 2) // 3}"


def kor_usd(v: float) -> str:
    """16억 2,000만 달러 같은 한국어 금액 표기 (1억 이상은 백만 달러 단위 반올림)."""
    v = abs(v)
    if v < 1e4:
        return f"{v:,.0f}달러"
    if v < 1e8:
        return f"{round(v / 1e4):,}만 달러"
    mil = round(v / 1e6)
    jo, rest = divmod(mil, 10**6)
    eok, rem = divmod(rest, 100)
    parts = []
    if jo:
        parts.append(f"{jo:,}조")
    if eok:
        parts.append(f"{eok:,}억")
    if rem and not jo:
        parts.append(f"{rem * 100:,}만")
    return " ".join(parts) + " 달러"


def fmt_pct(p: float) -> str:
    x = p * 100
    return f"{x:+,.0f}%" if abs(x) >= 100 else f"{x:+.1f}%"


def topic_particle(word: str) -> str:
    """한글 이름 뒤에 붙일 '은/는'. 판단할 수 없으면 '은(는)'."""
    w = word.split("(")[0].strip() or word
    ch = w[-1]
    if "가" <= ch <= "힣":
        return "은" if (ord(ch) - 0xAC00) % 28 else "는"
    if ch.isdigit():
        return "은" if ch in "013678" else "는"
    return "은(는)"


def row_label(r: list) -> str:
    return f"${r[1]}" if r[1] else r[2].title()


def build_summary(fund: dict, q: dict) -> str:
    rows = [r for r in q["rows"] if r[4] == "" and r[5] == "SH"]  # 옵션·채권 제외
    total = q["total"]
    held = [r for r in rows if r[10] != "S"]
    top10 = sorted(held, key=lambda r: r[6], reverse=True)[:10]
    new = sorted((r for r in rows if r[10] == "N"), key=lambda r: r[6], reverse=True)[:5]
    up = sorted((r for r in rows if r[10] == "U"), key=lambda r: r[11], reverse=True)[:5]
    down = sorted((r for r in rows if r[10] == "D"), key=lambda r: r[11])[:5]
    sold = sorted((r for r in rows if r[10] == "S"), key=lambda r: r[8], reverse=True)[:5]
    pct = lambda r: (r[7] - r[9]) / r[9]
    up.sort(key=pct, reverse=True)
    down.sort(key=pct)

    name = fund.get("name_ko") or fund["name"]
    lines = [
        f"{name} {q['label']} 13F 보고서",
        f"(기준일 {q['period']} · 공시일 {q['filed']})",
        "",
    ]
    chg = f" (전분기 대비 {fmt_pct(total / q['prev_total'] - 1)})" if q.get("prev_total") else ""
    lines += [f"총 평가액: {kor_usd(total)}{chg} · 보유 종목 {q['count']:,}개", ""]
    lines.append("상위 10개 보유 종목:")
    lines += [f"{i}. {row_label(r)} : {kor_usd(r[6])} ({r[6] / total * 100:.1f}%)" for i, r in enumerate(top10, 1)]
    if new:
        lines += ["", "신규 편입 종목:"] + [f"• {row_label(r)} : {kor_usd(r[6])}" for r in new]
    if up:
        lines += ["", "보유 주식 수를 가장 많이 늘린 종목:"] + [f"• {row_label(r)} : {fmt_pct(pct(r))}" for r in up]
    if down:
        lines += ["", "보유 주식 수를 가장 많이 줄인 종목:"] + [f"• {row_label(r)} : {fmt_pct(pct(r))}" for r in down]
    if sold:
        lines += ["", "전량 매도 (직전 분기말 평가액):"] + [f"• {row_label(r)} : {kor_usd(r[8])}" for r in sold]

    buys = sorted((r for r in rows if r[10] in "NU"), key=lambda r: r[11], reverse=True)[:4]
    sells = sorted((r for r in rows if r[10] in "DS"), key=lambda r: r[11])[:4]
    if buys and sells:
        lines += [
            "",
            f"요약: {name}{topic_particle(name)} {', '.join(row_label(r) for r in sells)} 비중을 크게 줄이고, "
            f"{', '.join(row_label(r) for r in buys)} 비중을 늘렸습니다. (추정 매매액 기준)",
        ]
    lines += ["", "※ 출처: SEC EDGAR 13F-HR. 분기말 미국 상장 롱 포지션 기준이며 공매도·분기 중 매매는 반영되지 않습니다."]
    return "\n".join(lines)


# ---------------------------------------------------------------- 메인

def fetch_fund(fund: dict, cfg: dict, args, state: dict, stats: dict) -> tuple[dict, dict]:
    """1단계: 공시 목록 확인 + 원문 캐시.

    운용사가 수십 곳이면 보유 목록을 전부 들고 있기엔 크므로,
    여기서는 메타 정보와 CUSIP별 최대 평가액(티커 조회 우선순위용)만 돌려주고,
    분할·CUSIP 승계 판정용 통계(stats)를 채운다.
    """
    ua = cfg["sec_user_agent"]
    cik = str(int(fund["cik"]))
    want = int(cfg.get("quarters", 13))
    filings, sec_name = list_13f_filings(cik, ua, want)
    picked, weights, newer = [], {}, None
    for q in pick_quarters(filings):
        if len(picked) >= want:
            break
        data = load_quarter(cik, q, ua, refetch=args.refetch)
        if data is None:  # 비공개 처리 등으로 보유 내용이 아직 없는 분기
            continue
        picked.append(q)
        if newer is not None:
            collect_share_ratios(newer, data, stats)
        newer = data
        for h in data["holdings"]:
            if h[5] > weights.get(h[0], 0):
                weights[h[0]] = h[5]
    if len(picked) < 2:
        raise RuntimeError("비교할 13F 공시가 2개 미만입니다")
    name = fund.get("name") or sec_name.title()
    # 원본이든 정정이든 새로 접수된 게 있으면 '새 공시'로 본다.
    signature = ",".join(sorted(f["accession"] for q in picked for f in [q["main"], *q["amendments"]]))
    is_new = state.get(cik) != signature
    log(f"[{name}] {len(picked)}개 분기 · 최신 {picked[0]['period']} (공시 {picked[0]['main']['filed']})"
        + (" ★ 새 공시" if is_new else ""))
    meta = {"cik": cik, "name": name, "cfg": fund, "picked": picked,
            "is_new": is_new, "signature": signature}
    return meta, weights


def build_fund(meta: dict, cfg: dict, tickers: dict, preds: dict, splits: dict) -> dict:
    """2단계: 캐시에서 다시 읽어 분기 비교 + 티커 + 요약 텍스트까지 만든다."""
    ua = cfg["sec_user_agent"]
    loaded = [load_quarter(meta["cik"], q, ua) for q in meta["picked"]]

    quarters = []
    for cur, prev in zip(loaded, loaded[1:]):
        rows = compare(cur, prev, preds, splits)
        for r in rows:
            r[1] = (tickers.get(r[0]) or {}).get("t", "")
        quarters.append({
            "period": cur["period"],
            "label": quarter_label(cur["period"]),
            "filed": cur["filed"],
            "accession": cur["accession"],
            "url": cur["url"],
            "amendments": cur["amendments"],
            "total": sum(h[5] for h in cur["holdings"]),
            "count": len(cur["holdings"]),
            "prev_period": prev["period"],
            "prev_total": sum(h[5] for h in prev["holdings"]),
            "prev_count": len(prev["holdings"]),
            "rows": rows,
        })
    trend = [
        {"period": d["period"], "label": short_label(d["period"]),
         "total": sum(h[5] for h in d["holdings"]), "count": len(d["holdings"])}
        for d in reversed(loaded)
    ]
    f = meta["cfg"]
    fund = {"cik": meta["cik"], "name": meta["name"], "name_ko": f.get("name_ko", ""),
            "manager": f.get("manager", ""), "category": f.get("category", ""),
            "quarters": quarters, "trend": trend}
    for q in quarters:
        q["summary"] = build_summary(fund, q)
    return fund


def index_entry(fund: dict) -> dict:
    """개요 화면용 요약 한 줄."""
    q = fund["quarters"][0]
    eq = [r for r in q["rows"] if r[4] == "" and r[5] == "SH"]
    held = sorted((r for r in eq if r[10] != "S"), key=lambda r: r[6], reverse=True)
    buy = max((r for r in eq if r[10] in ("N", "U")), key=lambda r: r[11], default=None)
    sell = min((r for r in eq if r[10] in ("D", "S")), key=lambda r: r[11], default=None)
    lab = lambda r: r[1] or r[2].title()
    return {
        "cik": fund["cik"], "name": fund["name"], "name_ko": fund["name_ko"],
        "manager": fund["manager"], "category": fund["category"],
        "period": q["period"], "label": short_label(q["period"]), "filed": q["filed"],
        "total": q["total"], "prev_total": q["prev_total"], "count": q["count"],
        "n_new": sum(1 for r in q["rows"] if r[10] == "N"),
        "n_sold": sum(1 for r in q["rows"] if r[10] == "S"),
        "top": [[lab(r), r[6]] for r in held[:3]],
        "buy": [lab(buy), buy[11], buy[10]] if buy else None,
        "sell": [lab(sell), sell[11], sell[10]] if sell else None,
    }


def add_to_periods(periods: dict, fund: dict, links: dict) -> None:
    """종목별 화면용: 분기 → 종목(CUSIP) → 운용사별 보유·변동을 모은다 (주식만, 옵션·채권 제외).

    종목 한 건은 [티커, 종목명, 평탄 배열]이고 배열은 운용사마다
    (운용사 번호, 상태 0신규/1확대/2축소/3유지/4전량매도, 평가액, 주식 수, 직전 주식 수, 추정 매매액) 6칸이다.
    """
    def canon(c: str) -> str:
        for _ in range(5):  # 승계가 연달아 있어도 끝까지 따라간다
            if c not in links:
                break
            c = links[c]
        return c

    for q in fund["quarters"]:
        p = periods.setdefault(q["period"], {"funds": [], "secs": {}})
        fi = len(p["funds"])
        p["funds"].append([fund["cik"], q["total"]])
        merged: dict[str, list] = {}
        for r in q["rows"]:
            if r[4] or r[5] != "SH":
                continue
            cid = canon(r[0])
            m = merged.get(cid)
            if m is None:
                merged[cid] = [r[1], r[2], r[6], r[7], r[9], r[11]]
            else:  # 옛·새 CUSIP을 한 분기에 같이 들고 있는 드문 경우
                m[0] = m[0] or r[1]
                for i, j in ((2, 6), (3, 7), (4, 9), (5, 11)):
                    m[i] += r[j]
        for cid, (tick, name, value, shares, pshares, trade) in merged.items():
            status = 0 if pshares <= 0 else 4 if shares <= 0 else 1 if shares > pshares else 2 if shares < pshares else 3
            sec = p["secs"].setdefault(cid, [tick, name, []])
            sec[0] = sec[0] or tick
            sec[2] += [fi, status, value, shares, pshares, trade]


def write_periods(periods: dict) -> list[dict]:
    """최근 MAX_PERIODS개 분기를 data/period_<기준일>.js 로 쓰고 목록을 돌려준다."""
    keep = sorted(periods, reverse=True)[:MAX_PERIODS]
    for old in DATA_DIR.glob("period_*.js"):
        if old.stem.removeprefix("period_") not in keep:
            old.unlink()
    out = []
    for period in keep:
        p = periods[period]
        secs = [[cid, s[0], s[1], s[2]] for cid, s in p["secs"].items()]
        secs.sort(key=lambda s: sum(s[3][2::6]), reverse=True)
        blob = json.dumps({"period": period, "funds": p["funds"], "secs": secs},
                          ensure_ascii=False, separators=(",", ":"))
        (DATA_DIR / f"period_{period}.js").write_text(f"window.__13F_PERIOD({blob});\n", encoding="utf-8")
        out.append({"period": period, "label": short_label(period), "n": len(p["funds"])})
    return out


def publish() -> None:
    """index.html + data/ 를 gh-pages 브랜치에 한 커밋으로 덮어쓴다.

    이력 없는 커밋 하나를 강제 푸시하므로 매일 올려도 저장소가 커지지 않는다.
    main 브랜치(코드)와는 별개이며, 작업 폴더의 체크아웃 상태를 건드리지 않는다.
    """
    h = hashlib.sha1(TEMPLATE.read_bytes())
    for f in sorted(DATA_DIR.glob("*.js")):
        h.update(f.read_bytes())
    backtest_page = ROOT / "backtest" / "index.html"
    if backtest_page.exists():  # 가격이 매일 바뀌므로 백테스트가 돌면 거의 매 거래일 게시된다
        h.update(backtest_page.read_bytes())
    sig, sig_path = h.hexdigest(), CACHE / "published.sig"
    if sig_path.exists() and sig_path.read_text() == sig:
        log("게시: 바뀐 내용이 없어 생략")
        return

    index_file = ROOT / ".git" / "site-index"  # 평소 인덱스와 섞이지 않게 게시 전용 인덱스를 쓴다
    index_file.unlink(missing_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}

    def git(*cmd: str) -> str:
        return subprocess.run(["git", *cmd], cwd=ROOT, env=env, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()

    git("add", "-f", "index.html", "data", ".nojekyll", *(["backtest"] if backtest_page.exists() else []))
    commit = git("commit-tree", git("write-tree"), "-m", f"Update {datetime.now():%Y-%m-%d %H:%M}")
    # 원격 이름 대신 주소로 올린다: 로컬에 추적 브랜치(origin/gh-pages)가 생기지 않아야
    # 아래 prune이 방금 올린 객체들을 지울 수 있고, .git이 게시할 때마다 불어나지 않는다.
    git("push", "-q", "-f", git("remote", "get-url", "origin"), f"{commit}:refs/heads/gh-pages")
    index_file.unlink(missing_ok=True)
    git("prune")
    sig_path.write_text(sig)
    log("게시: gh-pages 갱신 완료")


def resolve_tickers(weights: dict, cfg: dict, args) -> tuple[dict, int]:
    """평가액이 큰 CUSIP부터 티커를 조회한다. (티커 캐시, 남은 미조회 수)를 돌려준다."""
    cache_path = CACHE / "tickers.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    today = date.today()

    def needs_lookup(c: str) -> bool:
        hit = cache.get(c)
        if hit is None:
            return True
        if hit["t"]:
            return False
        return (today - date.fromisoformat(hit["d"])).days >= TICKER_RETRY_DAYS

    todo = sorted((c for c in weights if needs_lookup(c)), key=lambda c: weights[c], reverse=True)
    if todo and not args.no_tickers:
        limit = len(todo) if args.all_tickers else int(cfg.get("ticker_lookup_max", 500))
        batch = todo[:limit]
        log(f"티커 조회: {len(batch)}개 (미조회 총 {len(todo)}개)")
        lookup_tickers(batch, cache, cfg.get("openfigi_api_key", ""))
    return cache, sum(1 for c in weights if c not in cache)


def main() -> int:
    ap = argparse.ArgumentParser(description="13F 공시 대시보드 생성")
    ap.add_argument("--all-tickers", action="store_true", help="남은 CUSIP 티커를 전부 조회")
    ap.add_argument("--no-tickers", action="store_true", help="티커 조회 생략")
    ap.add_argument("--refetch", action="store_true", help="공시 캐시 무시")
    ap.add_argument("--publish", action="store_true", help="생성 후 GitHub Pages(gh-pages)에 게시")
    ap.add_argument("--no-backtest", action="store_true", help="따라하기 백테스트 생략")
    ap.add_argument("--notify", action="store_true", help="새 공시를 텔레그램으로 알림 (notify.py)")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    ua_file = ROOT / "sec_user_agent.txt"
    cfg["sec_user_agent"] = os.environ.get("SEC_USER_AGENT") or (
        ua_file.read_text(encoding="utf-8").strip() if ua_file.exists() else "")
    if "@" not in cfg["sec_user_agent"]:
        raise SystemExit("SEC 요청용 연락처가 없습니다. sec_user_agent.txt 에 '이름 이메일' 형식으로 한 줄 넣어 주세요.")
    for d in (CACHE, OUT, DATA_DIR):
        d.mkdir(exist_ok=True)
    log(f"=== {datetime.now():%Y-%m-%d %H:%M} 갱신 시작 · 운용사 {len(cfg['funds'])}곳 ===")

    state_path = CACHE / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    metas, weights, failed = [], {}, []
    stats = {"same": {}, "moved": {}, "sold": {}}
    for fund in cfg["funds"]:
        label = fund.get("name") or fund["cik"]
        try:
            meta, w = fetch_fund(fund, cfg, args, state, stats)
        except Exception as e:
            log(f"! {label} 수집 실패: {e}")
            failed.append(label)
            continue
        metas.append(meta)
        for c, v in w.items():
            if v > weights.get(c, 0):
                weights[c] = v
    if not metas:
        raise SystemExit("처리된 운용사가 없습니다.")

    tickers, remaining = resolve_tickers(weights, cfg, args)

    successions, splits = resolve_corporate_actions(stats)
    links = {old: new_cusip for old, (new_cusip, _) in successions.items()}
    preds: dict[str, list] = {}
    for old, (new_cusip, factor) in successions.items():
        preds.setdefault(new_cusip, []).append((old, factor))
    log(f"매매가 아닌 변화 보정: CUSIP 승계 {len(links)}건, 액면분할·병합 {len(splits)}건")

    index, new, periods, latest_only = [], [], {}, []
    for meta in metas:
        try:
            fund = build_fund(meta, cfg, tickers, preds, splits)
        except Exception as e:
            log(f"! {meta['name']} 처리 실패: {e}")
            failed.append(meta["name"])
            continue
        latest = fund["quarters"][0]
        (OUT / f"summary_{fund['cik']}_{latest['period']}.txt").write_text(latest["summary"] + "\n", encoding="utf-8")
        blob = json.dumps(fund, ensure_ascii=False, separators=(",", ":"))
        (DATA_DIR / f"fund_{fund['cik']}.js").write_text(f"window.__13F_LOAD({blob});\n", encoding="utf-8")
        index.append(index_entry(fund))
        add_to_periods(periods, fund, links)
        latest_only.append({**fund, "quarters": fund["quarters"][:1], "trend": []})  # 알림용 (메모리 절약)
        state[meta["cik"]] = meta["signature"]
        if meta["is_new"]:
            new.append(meta["name"])

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tickers_pending": remaining,
        "failed": failed,
        "funds": index,
        "periods": write_periods(periods),
        "links": links,
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace('"__DATA__"', blob, 1)
    DASHBOARD.write_text(html, encoding="utf-8")
    state_path.write_text(json.dumps(state), encoding="utf-8")

    size = sum(p.stat().st_size for p in DATA_DIR.glob("*.js")) / 1e6
    log(f"완료: {DASHBOARD.name} + data/ ({len(index)}곳, {size:.0f} MB)"
        + (f" · 새 공시 {len(new)}곳: {', '.join(new)}" if new else " · 새 공시 없음")
        + (f" · 실패 {len(failed)}곳: {', '.join(failed)}" if failed else "")
        + (f" · 티커 미조회 {remaining}개 (다음 실행 때 이어서 채움)" if remaining else ""))
    if args.notify:
        import notify
        notify.alert(latest_only, log)
    if not args.no_backtest:
        # 따라하기 백테스트(backtest/index.html). 실패해도 대시보드 게시는 계속하고, 지난 결과를 그대로 둔다.
        try:
            import backtest
            backtest.build()
        except Exception as e:
            log(f"! 백테스트 실패 (지난 결과 유지): {e}")
    if args.publish:
        try:
            publish()
        except subprocess.CalledProcessError as e:
            log(f"! 게시 실패: {(e.stderr or '').strip() or e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
