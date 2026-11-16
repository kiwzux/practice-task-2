"""Восстановление процесса (Process Discovery) по журналу событий.

Запуск:  python3 analyze.py        (нужен pandas; для картинок — Graphviz `dot`)
Вход:    "students (2).csv" рядом со скриптом
Выход:   variants.csv               — таблица полных вариантов
         student_transitions.csv    — пары непосредственно следующих активностей
         run_log.txt                — все проверки и цифры, на которые ссылается отчёт
         process_model.dot/.png/.svg — восстановленная вручную схема (BPMN-подобная)
         dfg.dot/.png               — автоматический граф непосредственно следующих (DFG)
         alpha_net.dot/.png         — сеть Петри, полученная алгоритмом Alpha (своя реализация)
"""
import itertools
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
SRC = HERE / "students (2).csv"
LOG_LINES = []

SHORT = {
    "Регистрация заявки": "R",
    "Первичная проверка": "C",
    "Согласование руководителем": "M",
    "Доработка заявки": "D",
    "Проверка бюджета": "B",
    "Дополнительное согласование": "X",
    "Исполнение закупки": "P",
    "Заявка выполнена": "E_ok",
    "Заявка отклонена": "E_rej",
    "Заявка отменена": "E_can",
}


def log(*args):
    line = " ".join(str(a) for a in args)
    print(line)
    LOG_LINES.append(line)


def section(title):
    log("")
    log("=" * 78)
    log(title)
    log("=" * 78)


# ------------------------------------------------------------ загрузка
def load():
    df = pd.read_csv(SRC, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    df["EventIndex"] = df["EventIndex"].astype(int)          # числовой порядок!
    df["Amount"] = df["Amount"].astype(float)
    df["ts"] = pd.to_datetime(df["Timestamp"])
    return df.sort_values(["ProcessInstanceId", "EventIndex"]).reset_index(drop=True)


def quality(df):
    section("1. Проверка данных")
    g = df.groupby("ProcessInstanceId")
    log("событий:", len(df), "| экземпляров:", df.ProcessInstanceId.nunique(),
        "| TemplateVersion:", sorted(df.TemplateVersion.unique()))
    log("EventId уникальны:", df.EventId.is_unique)
    log("EventIndex = 1..n без пропусков в каждом экземпляре:",
        g.EventIndex.apply(lambda s: list(s) == list(range(1, len(s) + 1))).all())
    log("время не убывает по EventIndex внутри экземпляра:",
        g.ts.apply(lambda s: s.is_monotonic_increasing).all())
    log("ровно один process_started и один process_completed на экземпляр:",
        (g.EventType.apply(lambda s: (s == "process_started").sum()) == 1).all(),
        (g.EventType.apply(lambda s: (s == "process_completed").sum()) == 1).all())
    log("process_started всегда первый, process_completed всегда последний:",
        (g.EventType.first() == "process_started").all(),
        (g.EventType.last() == "process_completed").all())
    log("сумма неизменна внутри экземпляра:", (g.Amount.nunique() == 1).all())
    log("одна ActivityId на одно ActivityName (и наоборот):",
        (df.groupby("ActivityName").ActivityId.nunique() == 1).all(),
        (df.groupby("ActivityId").ActivityName.nunique() == 1).all())
    t = df[df.EventType == "task_completed"]
    log("TaskId уникален у человеческих заданий (повтор = новое задание):", t.TaskId.is_unique)
    log("период журнала:", df.Timestamp.min(), "—", df.Timestamp.max())

    log("\nАктивность × тип события × роль × результат:")
    tab = (df.groupby(["ActivityName", "EventType", "Role", "Result"]).size()
             .rename("событий").reset_index())
    log(tab.to_string(index=False))

    # рабочий календарь
    h = t.ts
    in_hours = (h.dt.dayofweek < 5) & (h.dt.hour >= 9) & (
        (h.dt.hour < 18) | ((h.dt.hour == 18) & (h.dt.minute == 0) & (h.dt.second == 0)))
    log("\nчеловеческие завершения в рабочее время пн–пт 09–18:",
        f"{in_hours.sum()}/{len(t)}")
    df = df.copy()
    df["prev_ts"] = df.groupby("ProcessInstanceId").ts.shift()
    fin = df[df.EventType == "process_completed"]
    gap = (fin.ts - fin.prev_ts).dt.total_seconds()
    log("process_completed после последнего задания, секунд: min",
        gap.min(), "max", gap.max())


# ------------------------------------------------------------ трассы и варианты
def traces(df):
    tr = df.groupby("ProcessInstanceId").agg(
        route=("ActivityName", lambda s: " > ".join(s)),
        acts=("ActivityName", tuple),
        results=("Result", tuple),
        amount=("Amount", "first"),
        start=("ts", "first"),
        end=("ts", "last"),
    )
    tr["end_act"] = tr.acts.map(lambda a: a[-1])
    tr["returns"] = tr.acts.map(lambda a: a.count("Доработка заявки"))
    tr["has_X"] = tr.acts.map(lambda a: "Дополнительное согласование" in a)
    return tr


def variants(tr):
    section("2. Варианты (полные последовательности активностей)")
    vc = tr.groupby("route").size().sort_values(ascending=False)
    n = len(tr)
    rows = []
    for i, (route, cnt) in enumerate(vc.items(), 1):
        ex = tr[tr.route == route].index[:2].tolist()
        rows.append({"Variant": f"V{i}", "Route": route,
                     "NumberOfCases": int(cnt), "Share": round(cnt / n, 4)})
        short = " ".join(SHORT[a] for a in route.split(" > "))
        log(f"V{i}: {cnt:4d} ({cnt / n:6.1%})  {short}")
        log(f"      примеры: {', '.join(ex)}")
    out = pd.DataFrame(rows)
    out.to_csv(HERE / "variants.csv", index=False, encoding="utf-8")
    log("сумма долей:", round(out.Share.sum(), 4), "| сумма случаев:", out.NumberOfCases.sum())
    log("накопленная доля трёх частых вариантов:", round(out.Share.head(3).sum(), 4))
    return out


# ------------------------------------------------------------ переходы и развилки
def transitions(df, tr):
    section("3. Начала, окончания, присутствие активностей")
    log("первая активность:", dict(Counter(a[0] for a in tr.acts)))
    log("вторая активность:", dict(Counter(a[1] for a in tr.acts)))
    log("последняя активность:", dict(Counter(a[-1] for a in tr.acts)))
    log("предпоследняя -> последняя:",
        dict(Counter((a[-2], a[-1]) for a in tr.acts)))

    log("\nВ скольких экземплярах встречается активность (всего / по исходу):")
    acts = list(SHORT)
    pres = pd.DataFrame({a: tr.acts.map(lambda x, a=a: a in x) for a in acts})
    pres["исход"] = tr.end_act.values
    tab = pres.groupby("исход")[acts].sum().T
    tab["всего"] = pres[acts].sum()
    tab["событий"] = [int(df.ActivityName.eq(a).sum()) for a in acts]
    log(tab.to_string())

    section("4. Граф непосредственно следующих (DFG) с результатом источника")
    df = df.copy()
    df["next"] = df.groupby("ProcessInstanceId").ActivityName.shift(-1)
    pairs = df.dropna(subset=["next"])
    dfg = pairs.groupby(["ActivityName", "next"]).size().rename("n").reset_index()
    log(dfg.sort_values("n", ascending=False).to_string(index=False))
    pd.DataFrame({"SourceActivity": dfg.ActivityName, "TargetActivity": dfg.next}) \
        .to_csv(HERE / "student_transitions.csv", index=False, encoding="utf-8")
    log("в student_transitions.csv пар:", len(dfg))

    log("\nРазвилки: активность, результат -> следующая активность")
    res = pairs.groupby(["ActivityName", "Result", "next"]).size().rename("n").reset_index()
    for a, d in res.groupby("ActivityName"):
        if d.next.nunique() > 1 or d.Result.nunique() > 1:
            log(d.to_string(index=False))
            log("-")
    log("Результат однозначно определяет следующую активность:",
        (pairs.groupby(["ActivityName", "Result"]).next.nunique() == 1)
        .loc[lambda s: s.index.get_level_values(0) != "Проверка бюджета"].all())

    log("\nСлияния (у каких активностей несколько предшественников):")
    for b, d in dfg.groupby("next"):
        if len(d) > 1:
            log(f"  {b} <- " + ", ".join(f"{a} ({n})" for a, n in zip(d.ActivityName, d.n)))
    return dfg


def budget_split(tr):
    section("5. Развилка после «Проверки бюджета»: от чего зависит доп. согласование")
    b = tr[tr.acts.map(lambda a: "Проверка бюджета" in a)]
    log(b.groupby("has_X").amount.describe().round(2).to_string())
    lo = b[~b.has_X].amount.max()
    hi = b[b.has_X].amount.min()
    log(f"макс. сумма БЕЗ доп. согласования: {lo:.2f}; мин. сумма С ним: {hi:.2f}")
    for thr in (150000,):
        ok = ((b.amount >= thr) == b.has_X).all()
        log(f"правило «доп. согласование ⇔ Amount ≥ {thr}» верно для всех {len(b)} "
            f"дошедших до бюджета: {ok}")
    near = b[(b.amount > 140000) & (b.amount < 160000)].sort_values("amount")
    log("ближайшие к границе экземпляры:")
    log(pd.concat([near[~near.has_X].tail(3), near[near.has_X].head(3)])
        [["amount", "has_X"]].to_string())
    log("медианы суммы по исходу и числу возвратов (не доказывают независимость):")
    log(tr.groupby("end_act").amount.median().round(0).to_string())
    log(tr.groupby("returns").amount.median().round(0).to_string())


def loops(df, tr):
    section("6. Возвраты и повторы")
    log("распределение числа «Доработки заявки» на экземпляр:",
        tr.returns.value_counts().sort_index().to_dict())
    log("всего событий доработки:", int(tr.returns.sum()),
        "| экземпляров с возвратом:", int((tr.returns > 0).sum()))
    log("повторяющийся фрагмент после возврата всегда один и тот же:",
        set(tuple(a[i:i + 4]) for a in tr.acts for i in range(len(a))
            if a[i] == "Согласование руководителем" and i + 1 < len(a)
            and a[i + 1] == "Доработка заявки"))
    rep = Counter()
    for a in tr.acts:
        for act, k in Counter(a).items():
            if k > 1:
                rep[act] = max(rep[act], k)
    log("какие активности повторяются (макс. кратность):", dict(rep))

    # решения по проходу (1-й, 2-й, 3-й раз)
    rows = []
    for pid, d in df.groupby("ProcessInstanceId"):
        k = {"Первичная проверка": 0, "Согласование руководителем": 0}
        for a, r in zip(d.ActivityName, d.Result):
            if a in k:
                k[a] += 1
                rows.append((a, k[a], r))
    p = pd.DataFrame(rows, columns=["activity", "проход", "result"])
    log("\nРезультаты проверки/согласования по номеру прохода:")
    log(pd.crosstab([p.activity, p.проход], p.result, margins=True).to_string())

    # Ориентировочный расчёт при независимых решениях и неизменной вероятности.
    log("\nВероятности ниже условны: предполагаются независимые решения и неизменные вероятности;")
    log("повторные решения одной заявки могут зависеть друг от друга и от доработки.")
    m1 = p[(p.activity == "Согласование руководителем") & (p.проход == 1)]
    mR = p[(p.activity == "Согласование руководителем") & (p.проход > 1)]
    c1 = p[(p.activity == "Первичная проверка") & (p.проход == 1)]
    cR = p[(p.activity == "Первичная проверка") & (p.проход > 1)]
    q_m = m1.result.isin(["reject", "cancel"]).mean()
    q_c = (c1.result == "reject").mean()
    log(f"\nP(руководитель reject/cancel) на 1-м проходе = {q_m:.3f}; "
        f"на повторных: {mR.result.isin(['reject', 'cancel']).sum()}/{len(mR)}")
    log(f"  если бы вероятность не менялась, P(0 из {len(mR)}) = {(1 - q_m) ** len(mR):.1e}")
    log(f"P(первичная проверка reject) на 1-м проходе = {q_c:.3f}; "
        f"на повторных: {(cR.result == 'reject').sum()}/{len(cR)}")
    log(f"  если бы вероятность не менялась, P(0 из {len(cR)}) = {(1 - q_c) ** len(cR):.1e}")
    q_ret2 = (p[(p.activity == "Согласование руководителем") & (p.проход == 2)].result == "return").mean()
    n2 = int((tr.returns == 2).sum())
    log(f"P(возврат на 2-м проходе) = {q_ret2:.3f}; экземпляров, дошедших до 3-го прохода: {n2};"
        f" ожидалось бы 3-х возвратов ≈ {n2 * q_ret2:.1f}, P(0) = {(1 - q_ret2) ** n2:.2f}")

    share_big = (tr.amount >= 150000).mean()
    two = tr[tr.returns == 2]
    log(f"\nсумма ≥ 150000 у {share_big:.1%} всех заявок; у 2-кратно возвращённых: "
        f"{(two.amount >= 150000).sum()}/{len(two)} (макс. {two.amount.max():.2f}); "
        f"P(0 из {len(two)}) при той же доле = {(1 - share_big) ** len(two):.3f}")
    one = tr[tr.returns == 1]
    log(f"у 1-кратно возвращённых сумма ≥ 150000: {(one.amount >= 150000).sum()}/{len(one)}")
    return p


def timing(df, tr):
    section("7. Время (дополнительно)")
    df = df.copy()
    df["wait_h"] = df.groupby("ProcessInstanceId").ts.diff().dt.total_seconds() / 3600
    t = df[df.EventType == "task_completed"]
    log("часы от предыдущего события до завершения задания (календарные, включают очередь и ночи):")
    log(t.groupby("ActivityName").wait_h.describe(percentiles=[.5, .9]).round(1).to_string())
    tr = tr.copy()
    tr["days"] = (tr.end - tr.start).dt.total_seconds() / 86400
    log("\nдлительность экземпляра, календарные дни, по варианту:")
    log(tr.groupby("route").days.median().round(1).rename(lambda r: " ".join(SHORT[a] for a in r.split(" > "))).to_string())


# ------------------------------------------------------------ автоматические модели
def alpha_miner(tr):
    """Классический алгоритм Alpha (van der Aalst) по множеству трасс."""
    log_set = set(tr.acts)
    T = sorted({a for t in log_set for a in t})
    Ti = {t[0] for t in log_set}
    To = {t[-1] for t in log_set}
    succ = {(t[i], t[i + 1]) for t in log_set for i in range(len(t) - 1)}
    causal = {(a, b) for a, b in succ if (b, a) not in succ}
    unrelated = lambda a, b: (a, b) not in succ and (b, a) not in succ

    def ok(A, B):
        return (all((a, b) in causal for a in A for b in B)
                and all(unrelated(x, y) for x in A for y in A)
                and all(unrelated(x, y) for x in B for y in B))

    X = set()
    subsets = lambda s: (frozenset(c) for r in range(1, 6) for c in itertools.combinations(s, r))
    for A in subsets(T):
        for B in subsets(T):
            if ok(A, B):
                X.add((A, B))
    Y = {(A, B) for A, B in X
         if not any((A <= A2 and B <= B2 and (A, B) != (A2, B2)) for A2, B2 in X)}
    return T, Ti, To, succ, causal, sorted(Y, key=lambda p: (sorted(p[0]), sorted(p[1])))


def dfg_allows(succ, starts, ends, max_len):
    """Все последовательности длиной ≤ max_len, которые допускает DFG."""
    out = set()
    stack = [(s,) for s in starts]
    while stack:
        p = stack.pop()
        if p[-1] in ends:
            out.add(p)
        if len(p) < max_len:
            stack.extend(p + (b,) for a, b in succ if a == p[-1])
    return out


def automatic(tr, dfg):
    section("8. Автоматическая модель (Alpha, DFG) и сравнение с ручной")
    T, Ti, To, succ, causal, Y = alpha_miner(tr)
    log("Alpha: начальные", Ti, "| конечные", To)
    log("Alpha: найдено мест (A -> B):", len(Y))
    for A, B in Y:
        log("   ", "{" + ", ".join(SHORT[a] for a in sorted(A)) + "}",
            "->", "{" + ", ".join(SHORT[b] for b in sorted(B)) + "}")
    # воспроизведение журнала сетью Alpha (игра фишек)
    def replay(trace):
        tokens = Counter({"i": 1})
        for t in trace:
            pre = ["i"] if t in Ti else []
            pre += [k for k, (A, B) in enumerate(Y) if t in B]
            if any(tokens[q] < 1 for q in pre):
                return False
            for q in pre:
                tokens[q] -= 1
            for k, (A, B) in enumerate(Y):
                if t in A:
                    tokens[k] += 1
            if t in To:
                tokens["o"] += 1
        tokens = +tokens
        return tokens == Counter({"o": 1})
    fit = sum(replay(t) for t in tr.acts)
    log(f"Alpha-сеть воспроизводит без ошибок {fit}/{len(tr)} трасс журнала")
    par = {(a, b) for a, b in succ if (b, a) in succ}
    log("пар параллельности (a||b) в footprint:", len(par), "| коротких циклов a>b>a:", par)

    observed = set(tr.acts)
    L = max(map(len, observed)) + 3
    allowed = dfg_allows(succ, Ti, To, L)
    extra = allowed - observed
    log(f"\nDFG допускает {len(allowed)} разных трасс длиной ≤ {L}; в журнале {len(observed)}; "
        f"лишних {len(extra)}")
    cnt = Counter()
    for t in extra:
        kinds = []
        r = t.count("Доработка заявки")
        if r >= 3:
            kinds.append("3+ возвратов")
        if r >= 1:
            i = max(i for i, a in enumerate(t) if a == "Доработка заявки")
            if t[-1] in ("Заявка отклонена", "Заявка отменена"):
                kinds.append("отказ/отмена после возврата")
        if r == 2 and "Дополнительное согласование" in t:
            kinds.append("2 возврата + доп. согласование")
        cnt[" & ".join(kinds) or "другое"] += 1
    log("классы лишнего поведения:", dict(cnt))
    for t in sorted(extra, key=len)[:5]:
        log("   пример:", " ".join(SHORT[a] for a in t))
    log("DFG не выражает условие по сумме: ребро B->X и B->P допустимо при любой сумме.")
    return T, Ti, To, Y


# ------------------------------------------------------------ рисунки
def render(name, dot_src):
    (HERE / f"{name}.dot").write_text(dot_src, encoding="utf-8")
    if shutil.which("dot"):
        for fmt in ("png", "svg"):
            subprocess.run(["dot", f"-T{fmt}", "-Gdpi=150" if fmt == "png" else "-Gdpi=72",
                            str(HERE / f"{name}.dot"), "-o", str(HERE / f"{name}.{fmt}")],
                           check=True)


def draw_manual(tr, dfg):
    n = len(tr)
    e = {(a, b): int(k) for a, b, k in dfg.itertuples(index=False)}
    pres = {a: int(tr.acts.map(lambda x, a=a: a in x).sum()) for a in SHORT}
    ev = Counter(a for t in tr.acts for a in t)
    ret = tr.returns.value_counts().to_dict()

    def task(key, name, role):
        k = ev[name]
        rep = f"<BR/><FONT POINT-SIZE='9'>{k} событий в {pres[name]} экз.</FONT>" if k != pres[name] \
            else f"<BR/><FONT POINT-SIZE='9'>{pres[name]} экз. ({pres[name] / n:.0%})</FONT>"
        return (f'{key} [shape=box style="rounded,filled" fillcolor="#e8f0fe" '
                f'label=<<B>{name}</B><BR/><FONT POINT-SIZE="9" COLOR="#555555">{role}</FONT>{rep}>];')

    src = f"""digraph G {{
rankdir=TB; fontname="DejaVu Sans"; nodesep=0.3; ranksep=0.16;
node [fontname="DejaVu Sans" fontsize=11]; edge [fontname="DejaVu Sans" fontsize=9];
label=<<B>Восстановленный процесс «Заявка на закупку»</B><BR/>{n} экземпляров, TemplateVersion 1<BR/><FONT POINT-SIZE="9">Порог 150 000 — гипотеза, согласующаяся с журналом</FONT>>; labelloc=t; fontsize=15;
start [shape=circle label="" width=0.35 style=filled fillcolor="#c8e6c9"];
R  [shape=box style="rounded,filled" fillcolor="#eeeeee" label=<<B>Регистрация заявки</B><BR/><FONT POINT-SIZE="9">system, process_started</FONT><BR/><FONT POINT-SIZE="9">{n} экз. (100%)</FONT>>];
J1 [shape=diamond label="X" width=0.3 height=0.3 fixedsize=true style=filled fillcolor="#fff3cd" xlabel="слияние"];
{task("C", "Первичная проверка", "specialist")}
G1 [shape=diamond label="X" width=0.3 height=0.3 fixedsize=true style=filled fillcolor="#fff3cd" xlabel="Result"];
{task("M", "Согласование руководителем", "manager")}
G2 [shape=diamond label="X" width=0.3 height=0.3 fixedsize=true style=filled fillcolor="#fff3cd" xlabel="Result"];
{task("D", "Доработка заявки", "requester")}
{task("B", "Проверка бюджета", "finance")}
G3 [shape=diamond label="X" width=0.3 height=0.3 fixedsize=true style=filled fillcolor="#fff3cd" xlabel="Amount"];
{task("X", "Дополнительное согласование", "director")}
J3 [shape=diamond label="X" width=0.3 height=0.3 fixedsize=true style=filled fillcolor="#fff3cd"];
{task("P", "Исполнение закупки", "procurement")}
Eok  [shape=box style="rounded,filled" fillcolor="#c8e6c9" label=<<B>Заявка выполнена</B><BR/><FONT POINT-SIZE="9">system, fulfilled</FONT><BR/><FONT POINT-SIZE="9">{pres["Заявка выполнена"]} экз. ({pres["Заявка выполнена"] / n:.1%})</FONT>>];
Erej [shape=box style="rounded,filled" fillcolor="#f8d7da" label=<<B>Заявка отклонена</B><BR/><FONT POINT-SIZE="9">system, rejected</FONT><BR/><FONT POINT-SIZE="9">{pres["Заявка отклонена"]} экз. ({pres["Заявка отклонена"] / n:.1%})</FONT>>];
Ecan [shape=box style="rounded,filled" fillcolor="#ffe0b2" label=<<B>Заявка отменена</B><BR/><FONT POINT-SIZE="9">system, cancelled</FONT><BR/><FONT POINT-SIZE="9">{pres["Заявка отменена"]} экз. ({pres["Заявка отменена"] / n:.1%})</FONT>>];
end1 [shape=doublecircle label="" width=0.25 style=filled fillcolor="#333333"];
end2 [shape=doublecircle label="" width=0.25 style=filled fillcolor="#333333"];
end3 [shape=doublecircle label="" width=0.25 style=filled fillcolor="#333333"];
{{rank=same; D; C;}}
{{rank=same; Erej; M;}}
{{rank=same; Ecan; B;}}
{{rank=same; end1; legend;}}

start -> R;
R -> J1 [label="{e[("Регистрация заявки", "Первичная проверка")]}"];
J1 -> C;
C -> G1 [label="{ev["Первичная проверка"]}"];
G1 -> M [label="accept · {e[("Первичная проверка", "Согласование руководителем")]}" penwidth=2.5];
G1 -> Erej [label="reject · {e[("Первичная проверка", "Заявка отклонена")]}" color="#c62828" fontcolor="#c62828" constraint=false];
M -> G2 [label="{ev["Согласование руководителем"]}"];
G2 -> B [label="accept · {e[("Согласование руководителем", "Проверка бюджета")]}" penwidth=2.5];
G2 -> D [label="return · {e[("Согласование руководителем", "Доработка заявки")]}" color="#1565c0" fontcolor="#1565c0" constraint=false];
G2 -> Erej [label="reject · {e[("Согласование руководителем", "Заявка отклонена")]}" color="#c62828" fontcolor="#c62828" constraint=false];
G2 -> Ecan [label="cancel · {e[("Согласование руководителем", "Заявка отменена")]}" color="#ef6c00" fontcolor="#ef6c00" constraint=false];
D -> J1 [label="возврат · {e[("Доработка заявки", "Первичная проверка")]}\\n(1 раз: {ret.get(1, 0)} экз., 2 раза: {ret.get(2, 0)})" color="#1565c0" fontcolor="#1565c0" style=dashed constraint=false];
B -> G3 [label="{ev["Проверка бюджета"]}"];
G3 -> X [label="Amount ≥ 150 000 · {e[("Проверка бюджета", "Дополнительное согласование")]}"];
G3 -> J3 [label="Amount < 150 000 · {e[("Проверка бюджета", "Исполнение закупки")]}" penwidth=2.5];
X -> J3 [label="{e[("Дополнительное согласование", "Исполнение закупки")]}"];
J3 -> P;
P -> Eok [label="{e[("Исполнение закупки", "Заявка выполнена")]}" penwidth=2.5];
Eok -> end1; Erej -> end2; Ecan -> end3;

legend [fontsize=9 shape=plaintext label=<<TABLE BORDER="1" COLOR="#999999" CELLBORDER="0" CELLPADDING="3" CELLSPACING="0">
  <TR><TD COLSPAN="2"><B>Легенда</B></TD></TR>
  <TR><TD BGCOLOR="#e8f0fe" BORDER="1">задание</TD><TD ALIGN="LEFT">задание (task_completed); под названием — роль</TD></TR>
  <TR><TD BGCOLOR="#eeeeee" BORDER="1">system</TD><TD ALIGN="LEFT">автоматическое начало / окончание</TD></TR>
  <TR><TD BGCOLOR="#fff3cd" BORDER="1">◇ X</TD><TD ALIGN="LEFT">выбор одной ветки (XOR); условие на стрелке</TD></TR>
  <TR><TD>━━</TD><TD ALIGN="LEFT">основной маршрут; число — частота перехода</TD></TR>
  <TR><TD><FONT COLOR="#1565c0">- - -</FONT></TD><TD ALIGN="LEFT">повторное выполнение (цикл)</TD></TR>
  <TR><TD>N соб. в M экз.</TD><TD ALIGN="LEFT">повтор активности внутри экземпляра</TD></TR>
</TABLE>>];
Eok -> legend [style=invis];
node [group="main"]; start; R; J1; C; G1; M; G2; B; G3; J3; P; Eok; end1;
}}
"""
    render("process_model", src)


def draw_dfg(dfg):
    lines = ['digraph DFG { rankdir=LR; node [shape=box style=rounded fontname="DejaVu Sans" fontsize=10];',
             'edge [fontname="DejaVu Sans" fontsize=9];',
             'label="Автоматический DFG (все наблюдаемые пары a→b, число = частота)"; labelloc=t;']
    mx = dfg.n.max()
    for a, b, k in dfg.itertuples(index=False):
        lines.append(f'"{a}" -> "{b}" [label="{k}" penwidth={0.6 + 3 * k / mx:.2f}];')
    lines.append("}")
    render("dfg", "\n".join(lines))


def draw_alpha(T, Ti, To, Y):
    lines = ['digraph Alpha { rankdir=LR; fontname="DejaVu Sans"; node [fontname="DejaVu Sans" fontsize=10];',
             'label="Сеть Петри, построенная алгоритмом Alpha (переходы — прямоугольники, места — круги)"; labelloc=t;',
             'i [shape=circle label="i" style=filled fillcolor="#c8e6c9"]; o [shape=doublecircle label="o"];']
    for t in T:
        lines.append(f'"{t}" [shape=box style=filled fillcolor="#e8f0fe"];')
    for t in Ti:
        lines.append(f'i -> "{t}";')
    for t in To:
        lines.append(f'"{t}" -> o;')
    for k, (A, B) in enumerate(Y):
        lines.append(f'p{k} [shape=circle label="" width=0.25];')
        lines += [f'"{a}" -> p{k};' for a in A] + [f'p{k} -> "{b}";' for b in B]
    lines.append("}")
    render("alpha_net", "\n".join(lines))


def main():
    df = load()
    quality(df)
    tr = traces(df)
    variants(tr)
    dfg = transitions(df, tr)
    budget_split(tr)
    loops(df, tr)
    timing(df, tr)
    T, Ti, To, Y = automatic(tr, dfg)
    draw_manual(tr, dfg)
    draw_dfg(dfg)
    draw_alpha(T, Ti, To, Y)
    (HERE / "run_log.txt").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")


if __name__ == "__main__":
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    main()
