"""
Tests for restocking API endpoints.
"""
from datetime import date, timedelta

import pytest

import main


@pytest.fixture(autouse=True)
def clear_restock_orders():
    """Restock orders are stored in module memory, so reset them around each test."""
    main.restock_orders.clear()
    yield
    main.restock_orders.clear()


class TestRestockRecommendations:
    """Test suite for the restock recommendation endpoint."""

    def test_get_recommendations(self, client):
        """Test getting recommendations for a budget."""
        response = client.get("/api/restock/recommendations?budget=5000")
        assert response.status_code == 200

        data = response.json()
        assert data["budget"] == 5000
        assert isinstance(data["items"], list)
        assert len(data["items"]) > 0

        for item in data["items"]:
            for field in ["sku", "name", "quantity", "unit_cost", "line_total", "lead_time_days", "trend"]:
                assert field in item
            assert isinstance(item["quantity"], int)
            assert item["quantity"] > 0

    def test_recommendations_stay_within_budget(self, client):
        """Test that the recommended total never exceeds the budget."""
        for budget in [100, 1000, 2500, 5000, 50000]:
            data = client.get(f"/api/restock/recommendations?budget={budget}").json()
            assert data["total_cost"] <= budget
            assert abs(data["remaining_budget"] - (budget - data["total_cost"])) < 0.01

    def test_recommendations_total_calculation(self, client):
        """Test that line totals and the order total add up."""
        data = client.get("/api/restock/recommendations?budget=5000").json()

        for item in data["items"]:
            assert abs(item["line_total"] - item["quantity"] * item["unit_cost"]) < 0.01
        assert abs(data["total_cost"] - sum(i["line_total"] for i in data["items"])) < 0.01

    def test_recommendations_quantity_capped_at_demand_gap(self, client):
        """Test that quantities never exceed the forecast gap and skip items with no gap."""
        forecasts = {f["item_sku"]: f for f in client.get("/api/demand").json()}
        data = client.get("/api/restock/recommendations?budget=1000000").json()

        recommended = {i["sku"]: i for i in data["items"]}
        for sku, forecast in forecasts.items():
            gap = forecast["forecasted_demand"] - forecast["current_demand"]
            if gap <= 0:
                assert sku not in recommended
            else:
                # With an unlimited budget every positive gap is filled exactly
                assert recommended[sku]["quantity"] == gap

    def test_recommendations_prioritize_increasing_trend(self, client):
        """Test that increasing-trend items are ranked ahead of others."""
        data = client.get("/api/restock/recommendations?budget=1000000").json()
        trends = [i["trend"] for i in data["items"]]

        assert trends[0] == "increasing"
        first_non_increasing = next((n for n, t in enumerate(trends) if t != "increasing"), len(trends))
        assert all(t != "increasing" for t in trends[first_non_increasing:])

    def test_recommendations_zero_budget(self, client):
        """Test that a zero budget recommends nothing."""
        data = client.get("/api/restock/recommendations?budget=0").json()
        assert data["items"] == []
        assert data["total_cost"] == 0

    def test_recommendations_negative_budget(self, client):
        """Test that a negative budget is rejected."""
        response = client.get("/api/restock/recommendations?budget=-100")
        assert response.status_code == 422

    def test_recommendations_missing_budget(self, client):
        """Test that the budget parameter is required."""
        response = client.get("/api/restock/recommendations")
        assert response.status_code == 422


class TestRestockOrders:
    """Test suite for restock order endpoints."""

    def test_get_restock_orders_empty(self, client):
        """Test that no orders exist before any are submitted."""
        response = client.get("/api/restock-orders")
        assert response.status_code == 200
        assert response.json() == []

    def test_create_restock_order(self, client):
        """Test submitting a restock order."""
        payload = {"budget": 5000, "items": [{"sku": "WDG-001", "quantity": 10}, {"sku": "MTR-304", "quantity": 2}]}
        response = client.post("/api/restock-orders", json=payload)
        assert response.status_code == 201

        order = response.json()
        assert order["order_number"] == "RST-0001"
        assert order["status"] == "Submitted"
        assert len(order["items"]) == 2
        assert abs(order["total_cost"] - (10 * 12.5 + 2 * 185.0)) < 0.01

    def test_restock_order_lead_time_calculation(self, client):
        """Test that the order lead time is the slowest item's lead time."""
        forecasts = {f["item_sku"]: f for f in client.get("/api/demand").json()}
        skus = ["WDG-001", "CTL-330", "GSK-203"]
        payload = {"budget": 5000, "items": [{"sku": s, "quantity": 1} for s in skus]}

        order = client.post("/api/restock-orders", json=payload).json()

        expected_lead = max(forecasts[s]["lead_time_days"] for s in skus)
        assert order["lead_time_days"] == expected_lead
        submitted = date.fromisoformat(order["submitted_date"])
        assert order["expected_delivery"] == (submitted + timedelta(days=expected_lead)).isoformat()

    def test_restock_order_uses_server_prices(self, client):
        """Test that client-supplied prices are ignored."""
        payload = {"budget": 5000, "items": [{"sku": "WDG-001", "quantity": 4, "unit_cost": 0.01}]}
        order = client.post("/api/restock-orders", json=payload).json()
        assert order["items"][0]["unit_cost"] == 12.5

    def test_submitted_orders_listed_newest_first(self, client):
        """Test that submitted orders appear in the list, newest first."""
        client.post("/api/restock-orders", json={"budget": 100, "items": [{"sku": "WDG-001", "quantity": 1}]})
        client.post("/api/restock-orders", json={"budget": 100, "items": [{"sku": "GSK-203", "quantity": 1}]})

        orders = client.get("/api/restock-orders").json()
        assert [o["order_number"] for o in orders] == ["RST-0002", "RST-0001"]

    def test_create_restock_order_empty_items(self, client):
        """Test that an order with no items is rejected."""
        response = client.post("/api/restock-orders", json={"budget": 100, "items": []})
        assert response.status_code == 400
        assert "detail" in response.json()

    def test_create_restock_order_unknown_sku(self, client):
        """Test that an unknown SKU is rejected."""
        response = client.post("/api/restock-orders", json={"budget": 100, "items": [{"sku": "NOPE-999", "quantity": 1}]})
        assert response.status_code == 400
        assert "NOPE-999" in response.json()["detail"]

    def test_create_restock_order_invalid_quantity(self, client):
        """Test that a zero or negative quantity is rejected."""
        for quantity in [0, -5]:
            response = client.post("/api/restock-orders", json={"budget": 100, "items": [{"sku": "WDG-001", "quantity": quantity}]})
            assert response.status_code == 400
        assert client.get("/api/restock-orders").json() == []

    def test_create_restock_order_over_budget(self, client):
        """Test that an order costing more than its budget is rejected."""
        payload = {"budget": 1, "items": [{"sku": "CTL-330", "quantity": 10}]}
        response = client.post("/api/restock-orders", json=payload)
        assert response.status_code == 400
        assert "exceeds budget" in response.json()["detail"]

    def test_create_restock_order_duplicate_sku(self, client):
        """Test that a SKU listed twice is rejected."""
        payload = {"budget": 5000, "items": [{"sku": "WDG-001", "quantity": 1}, {"sku": "WDG-001", "quantity": 2}]}
        response = client.post("/api/restock-orders", json=payload)
        assert response.status_code == 400

    def test_create_restock_order_invalid_budget(self, client):
        """Test that non-finite or negative budgets are rejected without storing an order."""
        # Sent as raw JSON because the test client refuses to encode Infinity/NaN,
        # while the server's JSON parser accepts them.
        for budget in ["Infinity", "NaN", "-1"]:
            body = '{"budget": %s, "items": [{"sku": "WDG-001", "quantity": 1}]}' % budget
            response = client.post("/api/restock-orders", content=body,
                                   headers={"Content-Type": "application/json"})
            assert response.status_code == 422

        # A rejected order must not poison the stored list
        response = client.get("/api/restock-orders")
        assert response.status_code == 200
        assert response.json() == []


class TestRestockBudgetEdgeCases:
    """Test suite for unusual budget values on recommendations."""

    @pytest.mark.parametrize("budget", ["inf", "nan", "abc", "1000001"])
    def test_recommendations_rejects_invalid_budget(self, client, budget):
        """Test that non-finite, non-numeric and oversized budgets are rejected."""
        response = client.get(f"/api/restock/recommendations?budget={budget}")
        assert response.status_code == 422

    def test_recommendations_fractional_budget(self, client):
        """Test that a fractional budget is filled exactly to the cent."""
        # 12.50 widgets don't fit, one 9.25 filter does, and the 1.25 left over
        # is less than any remaining unit cost.
        data = client.get("/api/restock/recommendations?budget=10.50").json()
        assert [(i["sku"], i["quantity"]) for i in data["items"]] == [("FLT-405", 1)]
        assert data["total_cost"] == 9.25
        assert data["remaining_budget"] == 1.25
