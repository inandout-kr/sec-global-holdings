#!/usr/bin/env python3
"""13F 따라하기 백테스트.

update.py 가 만든 data/fund_<cik>.js 를 읽어, 공시가 나온 다음 거래일 시가에
그 운용사의 포트폴리오를 따라 샀다면 얼마를 벌었는지 계산하고
backtest/index.html 을 만든다. 가격은 Yahoo Finance 수정주가(yfinance).

전략
  copy : 공시 평가액 상위 TOP_N 종목을 평가액 비중대로 매수, 다음 공시 다음 거래일 시가에 전부 교체
  buys : 직전 분기 대비 신규(N)·확대(U) 종목만, 늘린 금액 비중대로 상위 TOP_N 종목
비교 : 같은 날 시가에 산 SPY

사용법:
    python backtest.py            # 가격 받아서 backtest/index.html 생성
update.py --publish 가 생성 뒤에 이 모듈의 build() 를 불러 함께 게시한다.
"""
from __future__ import annotations

import glob
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "backtest"
TEMPLATE = ROOT / "backtest_template.html"
TOP_N = 50
BENCH = "SPY"
HORIZONS = {"1W": 5, "1M": 21, "3M": 63, "6M": 126}


def log(msg: str) -> None:
    print(msg, flush=True)


def yahoo_ticker(t: str) -> str:
    return t.replace(".", "-").replace("/", "-").strip()


# ---------------------------------------------------------------- 공시 읽기

def load_filings() -> list[dict]:
    """운용사별로 공시를 오래된 순으로: 상위 보유(top)와 신규·확대 매수(buys)."""
    funds = []
    for path in sorted(glob.glob(str(DATA_DIR / "fund_*.js"))):
        s = Path(path).read_text(encoding="utf-8")
        j = json.loads(s[s.index("(") + 1:s.rindex(")")])
        fund = {k: j.get(k) for k in ("cik", "name", "name_ko", "manager", "category")}
        fund["filings"] = []
        for q in sorted(j["quarters"], key=lambda q: q["filed"]):
            agg: dict[str, dict] = {}
            for r in q["rows"]:
                # [cusip, ticker, name, class, put/call, SH/PRN, value, shares, prev_value, prev_shares, status, delta]
                if r[4] or r[5] != "SH" or not r[1] or r[6] <= 0:
                    continue
                t = yahoo_ticker(r[1])
                a = agg.setdefault(t, {"t": t, "n": r[2], "v": 0.0, "d": 0.0, "st": r[10]})
                a["v"] += r[6]
                a["d"] += r[11] or 0
            total = sum(a["v"] for a in agg.values())
            rows = sorted(agg.values(), key=lambda a: -a["v"])
            top = rows[:TOP_N]
            buys = sorted((a for a in rows if a["st"] in ("N", "U") and a["d"] > 0), key=lambda a: -a["d"])[:TOP_N]
            fund["filings"].append({
                "period": q["period"], "filed": q["filed"], "count": q["count"],
                "top": [[a["t"], a["n"], a["v"]] for a in top],
                "buys": [[a["t"], a["n"], a["d"]] for a in buys],
                "top_cov": sum(a["v"] for a in top) / total if total else 0.0,
            })
        if fund["filings"]:
            funds.append(fund)
    return funds


# ---------------------------------------------------------------- 가격

def load_prices(tickers: list[str], start: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """수정 시가·종가. 배당 반영으로 과거 값이 바뀌므로 매번 전체 기간을 다시 받는다."""
    import yfinance as yf

    opens, closes = [], []
    end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    for i in range(0, len(tickers), 200):
        d = yf.download(tickers[i:i + 200], start=start, end=end, auto_adjust=True,
                        progress=False, threads=True, group_by="column")
        if d is None or d.empty:
            continue
        opens.append(d["Open"])
        closes.append(d["Close"])
    O = pd.concat(opens, axis=1)
    C = pd.concat(closes, axis=1)
    O = O.loc[:, ~O.columns.duplicated()]
    C = C.loc[:, ~C.columns.duplicated()]
    C = C.dropna(how="all")
    return O.reindex(C.index), C


# ---------------------------------------------------------------- 백테스트

def run(funds: list[dict], O: pd.DataFrame, C: pd.DataFrame) -> dict:
    Cf = C.ffill()                    # 거래가 끊긴 종목은 마지막 가격으로 고정
    Of = O.where(O.notna(), Cf)       # 시가가 없으면 종가로 대신
    D = C.index
    if BENCH not in C.columns or C[BENCH].dropna().empty:
        raise RuntimeError(f"{BENCH} 가격이 없습니다")
    nD = len(D)

    def entry_idx(filed: str) -> int:
        return int(D.searchsorted(pd.Timestamp(filed), side="right"))  # 공시일 다음 거래일

    def port(items: list, a: int) -> tuple[dict, float]:
        total = sum(i[2] for i in items)
        ok = [i for i in items if i[0] in Of.columns and not np.isnan(Of[i[0]].iloc[a])]
        s = sum(i[2] for i in ok)
        if s <= 0:
            return {}, 0.0
        return {i[0]: i[2] / s for i in ok}, (s / total if total else 0.0)

    def ret_between(w: dict, a: int, b: int | None, close_at: int | None = None) -> float | None:
        """a일 시가 매수 → b일 시가 매도 (b가 None이면 close_at 또는 마지막 날 종가)."""
        if not w:
            return None
        tk = list(w)
        wv = np.array([w[t] for t in tk])
        o0 = Of[tk].iloc[a].values
        if b is not None:
            px = Of[tk].iloc[b].values
        else:
            px = Cf[tk].iloc[nD - 1 if close_at is None else close_at].values
        return float((px / o0 - 1) @ wv)

    def stats(nav: pd.Series) -> dict:
        r = nav.pct_change().dropna()
        n = len(nav) - 1
        tr = float(nav.iloc[-1] / nav.iloc[0] - 1)
        sd = r.std() if len(r) > 5 else 0
        return {"tr": tr, "mdd": float((nav / nav.cummax() - 1).min()),
                "sharpe": float(r.mean() / sd * np.sqrt(252)) if sd > 0 else None, "days": n}

    spyC, spyO = Cf[BENCH], Of[BENCH]
    summary, curves, filings, contrib = [], {}, [], []
    for f in funds:
        fl = []
        for x in f["filings"]:
            a = entry_idx(x["filed"])
            if a < nD:
                fl.append({**x, "ei": a})
        dedup = {x["ei"]: x for x in fl}        # 같은 날 들어가는 공시는 마지막 것만
        fl = [dedup[k] for k in sorted(dedup)]
        if not fl:
            continue
        names = {t: n for x in fl for t, n, _ in x["top"] + x["buys"]}
        series, contr = {}, {}
        for strat in ("copy", "buys"):
            key = "top" if strat == "copy" else "buys"
            nav, idx, v = [], [], 1.0
            for k, x in enumerate(fl):
                a = x["ei"]
                b = fl[k + 1]["ei"] if k + 1 < len(fl) else None
                end = b if b is not None else nD
                w, _ = port(x[key], a)
                if not w:                       # 살 종목이 없으면 현금
                    nav += [v] * (end - a)
                    idx += list(range(a, end))
                    continue
                tk = list(w)
                wv = np.array([w[t] for t in tk])
                o0 = Of[tk].iloc[a].values
                nav += list(v * ((Cf[tk].iloc[a:end].values / o0) @ wv))
                idx += list(range(a, end))
                px = Of[tk].iloc[b].values if b is not None else Cf[tk].iloc[-1].values
                r = px / o0 - 1
                if strat == "copy":
                    for t, ri, wi in zip(tk, r, wv):
                        contr[t] = contr.get(t, 0.0) + v * wi * ri
                v *= 1 + float(r @ wv)
            series[strat] = pd.Series(nav, index=D[idx])
        a0 = fl[0]["ei"]
        series["spy"] = spyC.iloc[a0:] / spyO.iloc[a0]
        base = pd.Series([1.0], index=[D[a0] - pd.Timedelta(days=1)])
        st = {k: stats(pd.concat([base, s])) for k, s in series.items()}

        row = {"cik": f["cik"], "fund": f["name"], "fund_ko": f["name_ko"], "manager": f["manager"],
               "category": f["category"], "start": str(D[a0].date()), "filings": len(fl),
               "last_filed": fl[-1]["filed"], "last_period": fl[-1]["period"],
               "copy_ret": st["copy"]["tr"], "buys_ret": st["buys"]["tr"], "spy_ret": st["spy"]["tr"],
               "copy_excess": st["copy"]["tr"] - st["spy"]["tr"], "buys_excess": st["buys"]["tr"] - st["spy"]["tr"],
               "copy_mdd": st["copy"]["mdd"], "buys_mdd": st["buys"]["mdd"], "spy_mdd": st["spy"]["mdd"],
               "copy_sharpe": st["copy"]["sharpe"], "days": st["copy"]["days"]}
        # 공시별 기간 수익
        per_h: dict[str, list] = {h: [] for h in list(HORIZONS) + ["NEXT", "TD"]}
        for k, x in enumerate(fl):
            a = x["ei"]
            nxt = fl[k + 1]["ei"] if k + 1 < len(fl) else None
            wc, cov = port(x["top"], a)
            wb, _ = port(x["buys"], a)
            ws = {BENCH: 1.0}
            fr = {"cik": f["cik"], "key": f"{f['cik']}_{x['period']}", "period": x["period"], "filed": x["filed"],
                  "entry": str(D[a].date()), "positions": x["count"], "coverage": x["top_cov"] * cov,
                  "top5": " ".join(t for t, _, _ in x["top"][:5]), "buys5": " ".join(t for t, _, _ in x["buys"][:5])}
            for h, n in HORIZONS.items():
                ok = a + n - 1 < nD
                for lab, w in (("copy", wc), ("buys", wb), ("spy", ws)):
                    fr[f"{lab}_{h}"] = ret_between(w, a, None, a + n - 1) if ok else None
            for lab, w in (("copy", wc), ("buys", wb), ("spy", ws)):
                fr[f"{lab}_NEXT"] = ret_between(w, a, nxt) if nxt is not None else None
                fr[f"{lab}_TD"] = ret_between(w, a, None)
            for h in per_h:
                if fr[f"copy_{h}"] is not None:
                    per_h[h].append((fr[f"copy_{h}"], fr[f"buys_{h}"], fr[f"spy_{h}"]))
            filings.append(fr)
        for h, vals in per_h.items():
            c = [v[0] - v[2] for v in vals]
            b = [v[1] - v[2] for v in vals if v[1] is not None]
            row[f"c_ex_{h}"] = float(np.mean(c)) if c else None
            row[f"b_ex_{h}"] = float(np.mean(b)) if b else None
            row[f"n_{h}"] = len(vals)
        summary.append(row)
        s = pd.DataFrame(series)
        curves[f["cik"]] = {"d": [str(d.date()) for d in s.index],
                            "c": [round(v, 4) for v in s["copy"]], "b": [round(v, 4) for v in s["buys"]],
                            "s": [round(v, 4) for v in s["spy"]]}
        ranked = sorted(contr.items(), key=lambda kv: -kv[1])
        for t, v in ranked[:8]:
            if v > 0:
                contrib.append({"cik": f["cik"], "key": f"{f['cik']}_{t}", "ticker": t, "name": names.get(t, ""), "contrib": v, "side": "win"})
        for t, v in ranked[::-1][:8]:
            if v < 0:
                contrib.append({"cik": f["cik"], "key": f"{f['cik']}_{t}", "ticker": t, "name": names.get(t, ""), "contrib": v, "side": "lose"})
    return {"asof": str(D[-1].date()), "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": summary, "curves": curves, "filings": filings, "contrib": contrib}


def _round(o):
    if isinstance(o, float):
        return None if not np.isfinite(o) else round(o, 5)
    if isinstance(o, dict):
        return {k: _round(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v) for v in o]
    return o


def build() -> Path:
    funds = load_filings()
    if not funds:
        raise RuntimeError("data/fund_*.js 가 없습니다. update.py 를 먼저 실행하세요.")
    tickers = sorted({t for f in funds for x in f["filings"] for t, _, _ in x["top"] + x["buys"]} | {BENCH})
    first = min(x["filed"] for f in funds for x in f["filings"])
    start = (datetime.fromisoformat(first) - timedelta(days=10)).strftime("%Y-%m-%d")
    log(f"백테스트: 운용사 {len(funds)}곳 · 종목 {len(tickers)}개 가격 받는 중 ({start}~)")
    O, C = load_prices(tickers, start)
    res = _round(run(funds, O, C))
    blob = json.dumps(res, ensure_ascii=False, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    OUT_DIR.mkdir(exist_ok=True)
    html = TEMPLATE.read_text(encoding="utf-8").replace('"__BT_DATA__"', blob, 1)
    out = OUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    beat = sum(1 for r in res["summary"] if (r["copy_excess"] or 0) > 0)
    log(f"백테스트 완료: {out.relative_to(ROOT)} · 가격 기준일 {res['asof']} · SPY 이긴 곳 {beat}/{len(res['summary'])}"
        f" · 가격 있는 종목 {int(C.notna().any().sum())}/{len(tickers)}")
    return out


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    build()
