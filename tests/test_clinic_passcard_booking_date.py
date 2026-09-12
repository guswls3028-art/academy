"""Collect the real idcard/API lifecycle contract in the normal pytest CI suite.

The domain's legacy tests.py is not matched by pytest.ini's test_*.py pattern.
Keep one implementation of the scenarios, including assessment-date regressions.
"""

from apps.domains.clinic import tests as clinic_tests


class TestClinicPasscardBookingDate(clinic_tests.StudentClinicPermissionAPITest):
    pass
