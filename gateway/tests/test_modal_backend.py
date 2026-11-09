from unittest.mock import Mock
import modal
import pytest
from modal_bridge.app import ModalBackend

@pytest.fixture
def call(monkeypatch):
    call = Mock()
    monkeypatch.setattr(modal.FunctionCall, "from_id", Mock(return_value=call))
    return call

@pytest.mark.parametrize(("error", "status"), [
    (TimeoutError(), "pending"),
    (modal.exception.FunctionTimeoutError("timeout"), "failed"),
    (modal.exception.OutputExpiredError(), "expired"),
    (modal.exception.RemoteError("remote failure"), "failed"),
    (ValueError("private error"), "failed"),
])
def test_adapter_errors(call, error, status):
    call.get.side_effect = error
    result = ModalBackend().poll("fc-test")
    assert result["status"] == status
    assert "private error" not in str(result)
    call.get.assert_called_once_with(timeout=0)

def test_service_failure_is_retriable(call):
    call.get.side_effect = modal.exception.ServiceError("unavailable")
    with pytest.raises(modal.exception.ServiceError):
        ModalBackend().poll("fc-test")

def test_result_contract(call):
    for result in [object(), float('nan'), 'x' * 1_048_577]:
        call.get.return_value = result
        assert ModalBackend().poll("fc-test")["status"] == "failed"
    call.get.return_value = None
    assert ModalBackend().poll("fc-test") == {"status": "succeeded", "result": None}

def test_cancel_does_not_terminate_shared_containers(call):
    ModalBackend().cancel("fc-test")
    call.cancel.assert_called_once_with(terminate_containers=False)

def test_submit_uses_allowlisted_target_and_single_json_argument(monkeypatch):
    function = Mock()
    function.spawn.return_value.object_id = "fc-test"
    lookup = Mock(return_value=function)
    monkeypatch.setattr(modal.Function, "from_name", lookup)
    assert ModalBackend().submit(("demo", "square"), {"value": 3}) == "fc-test"
    lookup.assert_called_once_with("demo", "square")
    function.spawn.assert_called_once_with({"value": 3})
