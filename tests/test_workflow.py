import pytest

from shared.workflow import WorkflowGraph, WorkflowNode, Status


def test_add_node():
    g = WorkflowGraph()
    n = WorkflowNode(task="test task", node_key="test")
    g.add_node(n)
    assert n.id in g.nodes
    assert g.nodes[n.id].status == Status.PENDING


def test_add_edge():
    g = WorkflowGraph()
    n1 = WorkflowNode(task="task 1")
    n2 = WorkflowNode(task="task 2")
    g.add_node(n1)
    g.add_node(n2)
    g.add_edge(n1.id, n2.id)
    assert n2.id in g.edges[n1.id]


def test_set_node_attributes():
    g = WorkflowGraph()
    n = WorkflowNode(task="test")
    g.add_node(n)
    g.set_node_attributes(n.id, ticker="NVDA", period="5y")
    attrs = g.get_node_attributes(n.id)
    assert attrs["ticker"] == "NVDA"
    assert attrs["period"] == "5y"


def test_mark_completed():
    g = WorkflowGraph()
    n = WorkflowNode(task="test")
    g.add_node(n)
    g.mark_completed(n.id, {"result": "ok"})
    assert g.nodes[n.id].status == Status.COMPLETED
    assert g.nodes[n.id].result == {"result": "ok"}


def test_mark_failed():
    g = WorkflowGraph()
    n = WorkflowNode(task="test")
    g.add_node(n)
    g.mark_failed(n.id, "something broke")
    assert g.nodes[n.id].status == Status.FAILED
    assert g.nodes[n.id].error == "something broke"


def test_get_next_nodes():
    g = WorkflowGraph()
    n1 = WorkflowNode(task="task 1")
    n2 = WorkflowNode(task="task 2")
    g.add_node(n1)
    g.add_node(n2)
    g.add_edge(n1.id, n2.id)
    next_nodes = g.get_next_nodes(n1.id)
    assert len(next_nodes) == 1
    assert next_nodes[0].id == n2.id


@pytest.mark.asyncio
async def test_run_workflow():
    g = WorkflowGraph()
    n1 = WorkflowNode(task="task 1")
    n2 = WorkflowNode(task="task 2")
    g.add_node(n1)
    g.add_node(n2)
    g.add_edge(n1.id, n2.id)
    await g.run_workflow(n1.id)
    assert g.state == Status.COMPLETED


def test_run_workflow_single_node():
    g = WorkflowGraph()
    n = WorkflowNode(task="only task")
    g.add_node(n)
    import asyncio
    asyncio.run(g.run_workflow(n.id))
    assert g.state == Status.COMPLETED
