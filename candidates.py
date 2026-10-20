# -*- coding: utf-8 -*-
"""Генератор словаря кандидатов пароля, упорядоченный по вероятности."""

COMMON = [
    "123456", "12345678", "123456789", "1234567", "12345", "1234", "123",
    "1234567890", "qwerty", "qwerty123", "qwertyuiop", "1q2w3e4r",
    "1qaz2wsx", "qazwsx", "asdfgh", "zxcvbnm",
    "password", "password1", "Password1", "Passw0rd", "P@ssw0rd",
    "admin", "admin123", "Admin123",
    "111111", "000000", "222222", "333333", "555555", "777777",
    "88888888", "999999", "123123", "112233", "123321",
    "654321", "987654321", "iloveyou", "dragon", "monkey",
    "uzbekistan", "Uzbekistan", "uzbekiston", "tashkent", "Tashkent",
    "fargona", "Fargona", "fergana",
    "rahimov", "Rahimov", "rahimov123", "Rahimov123", "Rahimov2026",
    "rahimovschool", "RahimovSchool", "r-school", "rschool",
    "RSchool", "rs", "RS", "lms", "LMS", "school", "School", "edu",
    "ustoz", "Ustoz", "teacher", "Teacher", "kurator", "Kurator",
    "demo", "Demo", "test", "Test", "test123", "Test123",
]

def _cases(w):
    s = set()
    for v in (w, w.lower(), w.upper(), w.capitalize()):
        if v:
            s.add(v)
    return list(s)

def build_candidates(first_name, last_name, username, extra=None):
    fn = (first_name or "").strip()
    ln = (last_name or "").strip()
    un = (username or "").strip()
    seen = set()
    out = []

    def add(*vals):
        for v in vals:
            if v and v not in seen:
                seen.add(v)
                out.append(v)

    # 1) самые слабые/стандартные пароли первыми
    add(*COMMON)

    # 2) фамилия/имя/логин в разных регистрах
    base_tokens = set()
    for w in (fn, ln, un):
        base_tokens.update(_cases(w))

    # 3) сочетания имени и фамилии
    combos = set()
    for a in _cases(fn):
        for b in _cases(ln):
            combos.update((a + b, b + a, a + "." + b, a + "_" + b,
                           a + "-" + b, a + b[0], b[0] + a))
    if fn and ln:
        combos.add(fn[0].lower() + ln.lower())
        combos.add(ln.lower() + fn[0].lower())
        combos.add(fn[0].upper() + ln.capitalize())

    # 4) варианты логина
    if un:
        combos.update(_cases(un))

    # собираем базовые слова
    for w in base_tokens:
        add(w)
    for c in sorted(combos):
        add(c)

    # 5) приставки/суффиксы к базовым словам (цифры, годы, спецсимволы)
    sufx = [
        "", "1", "12", "123", "1234", "12345", "123456", "1234567",
        "12345678", "123456789", "0", "01", "001", "007", "11", "22",
        "777", "7777", "111", "222", "333", "7", "9", "90", "99",
        "!", "@", "#", ".", "_", "123!", "@123",
    ]
    years = [str(y) for y in range(1990, 2031)]
    phonesuffix = ["90", "99", "77", "88", "99890", "99899", "99877", "99888"]

    layered = []
    for w in list(out):
        layered.append(w)
        for s in sufx:
            layered.append(w + s)
        for y in years:
            layered.append(w + y)
        for p in phonesuffix:
            layered.append(w + p)
    # чистим дубли, сохраняя порядок
    seen2 = set()
    final = []
    for v in layered:
        if v and v not in seen2:
            seen2.add(v)
            final.append(v)

    # добавление из внешнего файла словарей (если есть)
    extra_items = []
    if extra:
        try:
            with open(extra, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and line not in seen:
                        seen.add(line)
                        extra_items.append(line)
        except Exception:
            pass

    final = extra_items + final
    return final
