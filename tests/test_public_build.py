"""Synthetic checks only: no KASE prices, issue terms, or research portfolio."""
import ast
import copy
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from calculations import bond_analytics, portfolio_analytics, stress_scenarios, ytm_from_price
from data_manager import (csv_bytes, excel_bytes, import_portfolio_bytes,
                          portfolio_json_bytes, save_portfolio_json, starter_bonds)
from day_count_conventions import KASE_30_360
from kase_pricing import ACT365F_PRICING_METHOD, KASE_PRICING_METHOD
from models import Bond
from schedule import generate_schedule

ROOT = Path(__file__).resolve().parents[1]
PAGES = ['Обзор', 'Портфель', 'Анализ облигаций', 'Стресс-тест',
         'Сценарии', 'Методология', 'Данные и экспорт']


def synthetic_bond(method=ACT365F_PRICING_METHOD):
    b = Bond(name='Вымышленный учебный пример', record_id='synthetic-public-test',
             face_value=1000.0, quantity=17, ytm_pct=13.7, coupon_rate_pct=9.5,
             valuation_date=date(2026, 11, 9), maturity_date=date(2028, 12, 10),
             issue_date=date(2025, 12, 10), coupon_frequency=1,
             coupon_type='fixed', included=True, is_model=True,
             schedule_kind='regular', regularity_confirmed=True,
             schedule_confirmed=True, schedule_status='model',
             schedule_source='Вымышленный пример для проверки',
             pricing_method=method,
             day_count=KASE_30_360 if method == KASE_PRICING_METHOD else 'Actual/365 Fixed')
    b.cashflows = generate_schedule(b, b.maturity_date, source=b.schedule_source)
    return b


@pytest.mark.parametrize('page', PAGES)
@pytest.mark.parametrize('populated', [False, True])
def test_app_pages(page, populated):
    at = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=30).run()
    assert not at.exception
    if populated:
        at.session_state['bonds'] = [synthetic_bond()]
        at.session_state['assessment_date'] = date(2026, 11, 9)
        at.session_state['weight_mode'] = 'По рыночной стоимости'
    at.radio[0].set_value(page).run()
    assert not at.exception, [e.message for e in at.exception]
    assert not any(t.label == 'Локальный путь JSON' for t in at.text_input)


@pytest.mark.parametrize('method', [ACT365F_PRICING_METHOD, KASE_PRICING_METHOD])
def test_price_yield_and_stress(method):
    b = synthetic_bond(method)
    original = copy.deepcopy(b.to_dict())
    metrics = bond_analytics(b, 'model')
    recovered = ytm_from_price(b.cashflows, b.valuation_date, metrics['dirty_price'],
                               b.coupon_frequency, pricing_method=b.pricing_method,
                               day_count=b.day_count)
    assert recovered == pytest.approx(b.ytm_pct, abs=1e-7)
    p = portfolio_analytics([b], 'По рыночной стоимости', 'model')
    assert p['complete']
    shocks = stress_scenarios([b], [-200, 0, 200])
    assert shocks[0]['exact_change_kzt'] > 0
    assert shocks[1]['exact_change_kzt'] == pytest.approx(0, abs=1e-8)
    assert shocks[2]['exact_change_kzt'] < 0
    assert b.to_dict() == original


@pytest.mark.parametrize('extension,exporter', [('csv', csv_bytes), ('xlsx', excel_bytes),
                                              ('json', portfolio_json_bytes)])
def test_export_roundtrip(extension, exporter):
    b = synthetic_bond()
    restored = import_portfolio_bytes('synthetic.' + extension, exporter([b])).bonds
    assert len(restored) == 1
    assert restored[0].cashflows == b.cashflows
    assert restored[0].ytm_pct == b.ytm_pct
    assert restored[0].quantity == b.quantity


def test_no_server_file_write(tmp_path):
    target = tmp_path / 'must-not-exist.json'
    with pytest.raises(ValueError, match='отключена'):
        save_portfolio_json(target, [synthetic_bond()])
    assert not target.exists()
    assert starter_bonds() == []


def test_sessions_are_separate():
    a = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=30).run()
    a.session_state['bonds'] = [synthetic_bond()]
    a.run()
    b = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=30).run()
    assert len(a.session_state['bonds']) == 1
    assert b.session_state['bonds'] == []
    assert not a.exception and not b.exception


def test_no_bundled_issue_references_or_network_clients():
    assert not (ROOT / 'hsbkb22.py').exists()
    assert not (ROOT / 'mum180_0009.py').exists()
    assert not (ROOT / 'kase_discovery.py').exists()
    assert not list(ROOT.rglob('secrets.toml'))
    assert not list(ROOT.rglob('*.json'))
    forbidden = {'requests', 'httpx', 'urllib', 'socket', 'subprocess'}
    for path in ROOT.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
            if isinstance(node, ast.Import):
                assert not {n.name.split('.')[0] for n in node.names} & forbidden
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split('.')[0] not in forbidden
