import asyncio

import ccxt
import pytest

from core.order_submission import OrderOutcomeUnknown, submit_and_confirm
from diagnostics.repository import InMemoryDiagnosticRepository
from diagnostics.service import IncidentService, configure_service


@pytest.mark.parametrize("query_status", ["filled", "unknown", "not_found"])
def test_ambiguous_submission_never_resends(query_status):
    class Exchange:
        writes = 0
        reads = 0
        async def create_order(self, *args):
            self.writes += 1
            raise ccxt.RequestTimeout("response lost")
        async def fetch_order(self, *args, **kwargs):
            self.reads += 1
            if query_status == "unknown":
                raise ccxt.RequestTimeout("query also timed out")
            if query_status == "not_found":
                raise ccxt.OrderNotFound("not visible yet")
            return {"id": "order1", "status": "closed", "filled": 1}
    async def scenario():
        client = Exchange()
        repo = InMemoryDiagnosticRepository()
        configure_service(IncidentService(repo))
        try:
            if query_status == "filled":
                order = await submit_and_confirm(client, "BTC/USDT:USDT", "market", "buy", 1, None, {"clOrdId": "intent1"})
                assert order["filled"] == 1
                assert not repo.list_incidents()
            else:
                with pytest.raises(OrderOutcomeUnknown):
                    await submit_and_confirm(client, "BTC/USDT:USDT", "market", "buy", 1, None, {"clOrdId": "intent1"})
                assert client.reads == 2
                assert repo.list_incidents()[0].event_type == "order_submission_unknown"
            assert client.writes == 1
        finally:
            configure_service(None)
    asyncio.run(scenario())
