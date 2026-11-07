#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запись статусов ЭТрН в Google Sheets (service account).

env: GOOGLE_SA_JSON (путь к ключу), SHEET_ID (id таблицы).
Если не настроено — молча пропускает (монитор продолжает работать).
"""
import os, json

try:
    import gspread
    from google.oauth2.service_account import Credentials
    _OK = True
except Exception:
    _OK = False

_client = None


def _ws():
    global _client
    if _client:
        return _client
    sa = os.environ.get('GOOGLE_SA_JSON')
    sid = os.environ.get('SHEET_ID')
    if not _OK or not sa or not sid:
        return None
    creds = Credentials.from_service_account_file(
        sa, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    gc = gspread.authorize(creds)
    _client = gc.open_by_key(sid).sheet1
    if not _client.row_values(1):
        _client.append_row(['ЭТрН', 'Маршрут', 'Грузоотпр.', 'Грузополуч.', 'Перевозчик', 'Водитель', 'Обновлено'])
    return _client


def _mark(t):
    return '✓' if t['signed'] else '—'


def upsert_row(etn):
    ws = _ws()
    if not ws:
        return
    row = [etn['number'], etn['route'],
           *[_mark(etn['titles'][k]) for k, _ in
             [('shipper', ''), ('consignee', ''), ('carrier', ''), ('driver', '')]],
           etn.get('updated_at') or '']
    cell = ws.find(etn['number']) if etn['number'] != '?' else None
    if cell:
        ws.update(f'A{cell.row}', [row])
    else:
        ws.append_row(row)
