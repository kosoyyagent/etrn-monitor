#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мониторинг статусов ЭТрН (Контур.Логистика) → уведомления в MAX + Google Sheets.

Прототип для заказа: экспедитор следит за титулами ЭТрН (грузоотправитель,
грузополучатель, водитель) и «пинает» участников, когда накладная висит.

Режимы:
  --demo   работает на встроенных фикстурах (без ключей Контура)
  normal   читает API Контур.Логистика (нужен KONTUR_API_KEY + KONTUR_ORG_ID)

Уведомления: MAX Bot API (MAX_BOT_TOKEN + MAX_CHAT_ID) и/или консоль.
Таблица: Google Sheets через service account (опционально, см. sheets.py).
"""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

STATE = Path(__file__).parent / 'state.json'
POLL_SECONDS = int(os.environ.get('POLL_SECONDS', '300'))
STALE_HOURS = float(os.environ.get('STALE_HOURS', '4'))

TITLE_KEYS = [          # титулы ЭТрН, за которыми следим
    ('shipper',   'Грузоотправитель'),
    ('consignee', 'Грузополучатель'),
    ('carrier',   'Перевозчик'),
    ('driver',    'Водитель'),
]


def now_utc():
    return datetime.now(timezone.utc)


def load_state():
    try:
        return json.loads(STATE.read_text(encoding='utf8'))
    except Exception:
        return {}


def save_state(s):
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding='utf8')


# ---------------------------------------------------------------- Контур API
def fetch_etns():
    """Список ЭТрН со статусами титулов. Возвращает [{id, number, route, titles:{key:{signed,signed_at,by}}, updated_at}]."""
    key = os.environ.get('KONTUR_API_KEY')
    org = os.environ.get('KONTUR_ORG_ID')
    if not key or not org:
        return demo_etns()
    # Контур.Логистика API (diadoc-style): GET /v1/etn?status=active
    req = urllib.request.Request(
        f'https://logistika.kontur.ru/api/v1/organizations/{org}/etns?status=active',
        headers={'X-Api-Key': key, 'Accept': 'application/json'})
    try:
        data = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        print(f'[kontur] HTTP {e.code}: {e.read()[:200]}')
        return []
    return [normalize_etn(x) for x in data.get('items', data if isinstance(data, list) else [])]


def normalize_etn(x):
    """Приводит ответ API к внутреннему виду. Поля уточняются по реальному ответу Контура."""
    titles = {}
    raw = x.get('titles') or x.get('signatures') or {}
    for key, label in TITLE_KEYS:
        t = raw.get(key) or {}
        titles[key] = {
            'signed': bool(t.get('signed') or t.get('signedAt')),
            'signed_at': t.get('signedAt'),
            'by': t.get('signedBy') or '',
        }
    return {
        'id': str(x.get('id') or x.get('etnId')),
        'number': x.get('number') or x.get('documentNumber') or '?',
        'route': x.get('route') or f"{x.get('from','?')} → {x.get('to','?')}",
        'titles': titles,
        'updated_at': x.get('updatedAt') or x.get('modifiedAt'),
    }


# ---------------------------------------------------------------- demo data
def demo_etns():
    return [
        {'id': 'ETN-1042', 'number': '1042', 'route': 'Москва → Казань',
         'updated_at': '2026-11-04T08:00:00Z',
         'titles': {'shipper':  {'signed': True,  'signed_at': '2026-11-04T08:15Z', 'by': 'ООО Груз'},
                    'consignee':{'signed': False, 'signed_at': None, 'by': ''},
                    'carrier': {'signed': True,  'signed_at': '2026-11-04T08:20Z', 'by': 'ИП Вези'},
                    'driver':  {'signed': True,  'signed_at': '2026-11-04T08:30Z', 'by': 'Сидоров'}}},
        {'id': 'ETN-1043', 'number': '1043', 'route': 'Казань → Уфа',
         'updated_at': '2026-11-03T14:00:00Z',
         'titles': {'shipper':  {'signed': True,  'signed_at': '2026-11-03T14:10Z', 'by': 'ООО Груз'},
                    'consignee':{'signed': True,  'signed_at': '2026-11-03T15:00Z', 'by': 'ООО Приём'},
                    'carrier': {'signed': True,  'signed_at': '2026-11-03T14:12Z', 'by': 'ИП Вези'},
                    'driver':  {'signed': False, 'signed_at': None, 'by': ''}}},
        {'id': 'ETN-1044', 'number': '1044', 'route': 'Уфа → Челябинск',
         'updated_at': '2026-11-04T10:00:00Z',
         'titles': {k: {'signed': True, 'signed_at': '2026-11-04T10:30Z', 'by': 'x'} for k, _ in TITLE_KEYS}},
    ]


# ---------------------------------------------------------------- логика
def diff_events(etn, st):
    """События по одной ЭТрН: новые подписи и зависшие титулы."""
    prev = st.get(etn['id'], {})
    evs = []
    cur = {}
    for key, label in TITLE_KEYS:
        t = etn['titles'][key]
        cur[key] = t['signed']
        if t['signed'] and not prev.get(key):
            evs.append(('signed', f"✅ {label} подписал ЭТрН №{etn['number']} ({etn['route']})"))
        elif not t['signed']:
            # висит: дата обновления старше STALE_HOURS и хоть кто-то уже подписал
            try:
                upd = datetime.fromisoformat(etn['updated_at'].replace('Z', '+00:00'))
                stale = (now_utc() - upd).total_seconds() / 3600 > STALE_HOURS
            except Exception:
                stale = False
            if stale and not prev.get(f'pinged_{key}'):
                evs.append(('stale', f"⏰ ЭТрН №{etn['number']} ждёт подписи: {label} (уже {STALE_HOURS:.0f}+ ч) — {etn['route']}"))
                cur[f'pinged_{key}'] = True
        if t['signed']:
            cur[f'pinged_{key}'] = True
    return evs, cur


def notify_max(text):
    tok = os.environ.get('MAX_BOT_TOKEN'); chat = os.environ.get('MAX_CHAT_ID')
    if not tok or not chat:
        print('[notify]', text); return
    try:
        body = json.dumps({'text': text}).encode()
        req = urllib.request.Request(
            f'https://botapi.max.ru/messages?access_token={tok}&chat_id={chat}',
            data=body, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print('[max] send failed:', str(e)[:120])


def hang_hours(etn):
    try:
        upd = datetime.fromisoformat(etn['updated_at'].replace('Z', '+00:00'))
        return max(0.0, (now_utc() - upd).total_seconds() / 3600)
    except Exception:
        return 0.0


def build_report(etns):
    """Дайджест по ТЗ: список накладных, статус, кто должен подписать, сколько висит."""
    lines = [f'Отчёт по ЭТрН — {now_utc():%d.%m %H:%M} UTC']
    done, pending = [], []
    for etn in etns:
        missing = [label for key, label in TITLE_KEYS if not etn['titles'][key]['signed']]
        if missing:
            pending.append((etn, missing))
        else:
            done.append(etn)
    lines.append(f'В работе: {len(etns)} | Завершено: {len(done)} | Ждут подписи: {len(pending)}')
    for etn, missing in pending:
        h = hang_hours(etn)
        flag = '🔴' if h > STALE_HOURS else '🟡'
        lines.append(f"{flag} №{etn['number']} {etn['route']} — ждёт: {', '.join(missing)} — висит {h:.1f} ч")
    if not pending:
        lines.append('Все накладные подписаны — можно выставлять счета.')
    return '\n'.join(lines)


def run_once():
    st = load_state()
    etns = fetch_etns()
    print(f'[{now_utc():%H:%MZ}] ЭТрН в работе: {len(etns)}')
    for etn in etns:
        evs, cur = diff_events(etn, st.get('etns', {}))
        st.setdefault('etns', {})[etn['id']] = cur
        for _, msg in evs:
            notify_max(msg)
        # состояние в таблицу
        try:
            from sheets import upsert_row
            upsert_row(etn)
        except Exception:
            pass
    save_state(st)


if __name__ == '__main__':
    if '--report' in sys.argv:
        notify_max(build_report(fetch_etns())); sys.exit(0)
    if '--demo' in sys.argv:
        run_once(); sys.exit(0)
    print('Мониторинг ЭТрН запущен (интервал', POLL_SECONDS, 'с). Ctrl+C — стоп.')
    while True:
        try:
            run_once()
        except Exception as e:
            print('[err]', str(e)[:150])
        time.sleep(POLL_SECONDS)
