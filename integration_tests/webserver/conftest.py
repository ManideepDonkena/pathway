import pytest

from pathway.tests.utils import UniquePortDispenser

PORT_DISPENSER = UniquePortDispenser()


@pytest.fixture
def port():
    yield PORT_DISPENSER.get_unique_port()
