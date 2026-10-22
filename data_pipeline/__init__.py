"""SXSVN data pipeline: подготовка данных о компаниях, платежах и связях.

Зона ответственности — Данил. Модуль превращает CSV кейса в SQLite-базу
(`python -m data_pipeline build`) и отдаёт данные через адаптеры,
совместимые с протоколами ``ai_engine.storage.base``:

- ``SqliteCompanyRepository``      -> ``CompanyRepository``
- ``PaymentRelationshipRepository`` -> ``RelationshipRepository``
- ``PaymentCalculationRepository``  -> ``CalculationRepository``

Принципы: ничего не выдумываем, у каждого признака есть происхождение
(файл/колонка/расчёт), отсутствие данных возвращается явно.
"""

from .config import DataSettings
from .exposure import PaymentCalculationRepository, calculate_sector_exposure
from .profiles import SqliteCompanyRepository
from .relationships import PaymentRelationshipRepository
from .store import connect

__all__ = [
    "DataSettings",
    "PaymentCalculationRepository",
    "PaymentRelationshipRepository",
    "SqliteCompanyRepository",
    "calculate_sector_exposure",
    "connect",
]
