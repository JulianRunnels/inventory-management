import math
from datetime import date, timedelta
from threading import Lock
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import List, Optional
from pydantic import BaseModel, Field
from mock_data import inventory_items, orders, demand_forecasts, backlog_items, spending_summary, monthly_spending, category_spending, recent_transactions, purchase_orders

app = FastAPI(title="Factory Inventory Management System")

# Quarter mapping for date filtering
QUARTER_MAP = {
    'Q1-2025': ['2025-01', '2025-02', '2025-03'],
    'Q2-2025': ['2025-04', '2025-05', '2025-06'],
    'Q3-2025': ['2025-07', '2025-08', '2025-09'],
    'Q4-2025': ['2025-10', '2025-11', '2025-12']
}

def filter_by_month(items: list, month: Optional[str]) -> list:
    """Filter items by month/quarter based on order_date field"""
    if not month or month == 'all':
        return items

    if month.startswith('Q'):
        # Handle quarters
        if month in QUARTER_MAP:
            months = QUARTER_MAP[month]
            return [item for item in items if any(m in item.get('order_date', '') for m in months)]
    else:
        # Direct month match
        return [item for item in items if month in item.get('order_date', '')]

    return items

def apply_filters(items: list, warehouse: Optional[str] = None, category: Optional[str] = None,
                 status: Optional[str] = None) -> list:
    """Apply common filters to a list of items"""
    filtered = items

    if warehouse and warehouse != 'all':
        filtered = [item for item in filtered if item.get('warehouse') == warehouse]

    if category and category != 'all':
        filtered = [item for item in filtered if item.get('category', '').lower() == category.lower()]

    if status and status != 'all':
        filtered = [item for item in filtered if item.get('status', '').lower() == status.lower()]

    return filtered

def _json_safe(value):
    """Replace non-finite floats, which JSON can't represent, with their string form"""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Same 422 shape as FastAPI's default handler, but safe to serialize.

    The default handler echoes the rejected input back. When a client sends
    Infinity or NaN (which the JSON parser accepts), that echo can't be encoded
    and the 422 turns into a 500.
    """
    return JSONResponse(status_code=422, content={"detail": _json_safe(jsonable_encoder(exc.errors()))})

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data models
class InventoryItem(BaseModel):
    id: str
    sku: str
    name: str
    category: str
    warehouse: str
    quantity_on_hand: int
    reorder_point: int
    unit_cost: float
    location: str
    last_updated: str

class Order(BaseModel):
    id: str
    order_number: str
    customer: str
    items: List[dict]
    status: str
    order_date: str
    expected_delivery: str
    total_value: float
    actual_delivery: Optional[str] = None
    warehouse: Optional[str] = None
    category: Optional[str] = None

class DemandForecast(BaseModel):
    id: str
    item_sku: str
    item_name: str
    current_demand: int
    forecasted_demand: int
    trend: str
    period: str
    unit_cost: float
    lead_time_days: int

class BacklogItem(BaseModel):
    id: str
    order_id: str
    item_sku: str
    item_name: str
    quantity_needed: int
    quantity_available: int
    days_delayed: int
    priority: str
    has_purchase_order: Optional[bool] = False

class PurchaseOrder(BaseModel):
    id: str
    backlog_item_id: str
    supplier_name: str
    quantity: int
    unit_cost: float
    expected_delivery_date: str
    status: str
    created_date: str
    notes: Optional[str] = None

class CreatePurchaseOrderRequest(BaseModel):
    backlog_item_id: str
    supplier_name: str
    quantity: int
    unit_cost: float
    expected_delivery_date: str
    notes: Optional[str] = None

class RestockLineItem(BaseModel):
    sku: str
    name: str
    quantity: int
    unit_cost: float
    line_total: float
    lead_time_days: int
    trend: str

class RestockRecommendation(BaseModel):
    budget: float
    items: List[RestockLineItem]
    total_cost: float
    remaining_budget: float

MAX_RESTOCK_BUDGET = 1_000_000

class RestockOrderItemRequest(BaseModel):
    sku: str
    quantity: int

class CreateRestockOrderRequest(BaseModel):
    # allow_inf_nan=False matters: the JSON parser accepts Infinity/NaN, and storing
    # one would make every later GET /api/restock-orders fail to serialize.
    budget: float = Field(ge=0, le=MAX_RESTOCK_BUDGET, allow_inf_nan=False)
    items: List[RestockOrderItemRequest]

class RestockOrder(BaseModel):
    id: str
    order_number: str
    items: List[RestockLineItem]
    total_cost: float
    budget: float
    status: str
    submitted_date: str
    lead_time_days: int
    expected_delivery: str

# Submitted restock orders live only in process memory, matching the rest of the
# app's no-database design; they are cleared whenever the server restarts.
restock_orders: List[dict] = []
# Sync endpoints run in a threadpool; the lock keeps order numbers unique when
# two orders are submitted at the same time.
restock_orders_lock = Lock()

# Lower rank = restock first. Rising demand is the most urgent to cover.
TREND_PRIORITY = {'increasing': 0, 'stable': 1, 'decreasing': 2}

def build_line_item(forecast: dict, quantity: int) -> dict:
    """Price a forecast item at the server-side unit cost"""
    return {
        'sku': forecast['item_sku'],
        'name': forecast['item_name'],
        'quantity': quantity,
        'unit_cost': forecast['unit_cost'],
        'line_total': round(quantity * forecast['unit_cost'], 2),
        'lead_time_days': forecast['lead_time_days'],
        'trend': forecast['trend'],
    }

def recommend_restock(budget: float) -> dict:
    """Greedily fill the budget with forecast items that have a demand gap"""
    candidates = [f for f in demand_forecasts if f['forecasted_demand'] > f['current_demand']]
    # Highest-urgency trend first, then the largest shortfall within each trend.
    candidates.sort(key=lambda f: (TREND_PRIORITY.get(f['trend'], len(TREND_PRIORITY)),
                                   -(f['forecasted_demand'] - f['current_demand'])))

    items = []
    # Work in integer cents so float error can't under- or over-fill a line
    # (e.g. 0.3 // 0.1 == 2.0 in floating point).
    remaining_cents = int(round(budget * 100))
    for forecast in candidates:
        gap = forecast['forecasted_demand'] - forecast['current_demand']
        cost_cents = int(round(forecast['unit_cost'] * 100))
        # Buy as much of the gap as the budget allows. An item that only partly fits
        # is partially filled, and we keep going (rather than stopping) so cheaper
        # items further down the list can still use the leftover budget.
        quantity = min(gap, remaining_cents // cost_cents)
        if quantity <= 0:
            continue
        items.append(build_line_item(forecast, quantity))
        remaining_cents -= quantity * cost_cents

    total_cost = round(sum(line['line_total'] for line in items), 2)
    return {
        'budget': budget,
        'items': items,
        'total_cost': total_cost,
        'remaining_budget': round(budget - total_cost, 2),
    }

# API endpoints
@app.get("/")
def root():
    return {"message": "Factory Inventory Management System API", "version": "1.0.0"}

@app.get("/api/inventory", response_model=List[InventoryItem])
def get_inventory(
    warehouse: Optional[str] = None,
    category: Optional[str] = None
):
    """Get all inventory items with optional filtering"""
    return apply_filters(inventory_items, warehouse, category)

@app.get("/api/inventory/{item_id}", response_model=InventoryItem)
def get_inventory_item(item_id: str):
    """Get a specific inventory item"""
    item = next((item for item in inventory_items if item["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item

@app.get("/api/orders", response_model=List[Order])
def get_orders(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get all orders with optional filtering"""
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)
    return filtered_orders

@app.get("/api/orders/{order_id}", response_model=Order)
def get_order(order_id: str):
    """Get a specific order"""
    order = next((order for order in orders if order["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@app.get("/api/demand", response_model=List[DemandForecast])
def get_demand_forecasts():
    """Get demand forecasts"""
    return demand_forecasts

@app.get("/api/backlog", response_model=List[BacklogItem])
def get_backlog():
    """Get backlog items with purchase order status"""
    # Add has_purchase_order flag to each backlog item
    result = []
    for item in backlog_items:
        item_dict = dict(item)
        # Check if this backlog item has a purchase order
        has_po = any(po["backlog_item_id"] == item["id"] for po in purchase_orders)
        item_dict["has_purchase_order"] = has_po
        result.append(item_dict)
    return result

@app.get("/api/dashboard/summary")
def get_dashboard_summary(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get summary statistics for dashboard with optional filtering"""
    # Filter inventory
    filtered_inventory = apply_filters(inventory_items, warehouse, category)

    # Filter orders
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)

    total_inventory_value = sum(item["quantity_on_hand"] * item["unit_cost"] for item in filtered_inventory)
    low_stock_items = len([item for item in filtered_inventory if item["quantity_on_hand"] <= item["reorder_point"]])
    pending_orders = len([order for order in filtered_orders if order["status"] in ["Processing", "Backordered"]])
    total_backlog_items = len(backlog_items)

    return {
        "total_inventory_value": round(total_inventory_value, 2),
        "low_stock_items": low_stock_items,
        "pending_orders": pending_orders,
        "total_backlog_items": total_backlog_items,
        "total_orders_value": sum(order["total_value"] for order in filtered_orders)
    }

@app.get("/api/spending/summary")
def get_spending_summary():
    """Get spending summary statistics"""
    return spending_summary

@app.get("/api/spending/monthly")
def get_monthly_spending():
    """Get monthly spending breakdown"""
    return monthly_spending

@app.get("/api/spending/categories")
def get_category_spending():
    """Get spending by category"""
    return category_spending

@app.get("/api/spending/transactions")
def get_recent_transactions():
    """Get recent transactions"""
    return recent_transactions

@app.get("/api/reports/quarterly")
def get_quarterly_reports():
    """Get quarterly performance reports"""
    # Calculate quarterly statistics from orders
    quarters = {}

    for order in orders:
        order_date = order.get('order_date', '')
        # Determine quarter
        if '2025-01' in order_date or '2025-02' in order_date or '2025-03' in order_date:
            quarter = 'Q1-2025'
        elif '2025-04' in order_date or '2025-05' in order_date or '2025-06' in order_date:
            quarter = 'Q2-2025'
        elif '2025-07' in order_date or '2025-08' in order_date or '2025-09' in order_date:
            quarter = 'Q3-2025'
        elif '2025-10' in order_date or '2025-11' in order_date or '2025-12' in order_date:
            quarter = 'Q4-2025'
        else:
            continue

        if quarter not in quarters:
            quarters[quarter] = {
                'quarter': quarter,
                'total_orders': 0,
                'total_revenue': 0,
                'delivered_orders': 0,
                'avg_order_value': 0
            }

        quarters[quarter]['total_orders'] += 1
        quarters[quarter]['total_revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            quarters[quarter]['delivered_orders'] += 1

    # Calculate averages and fulfillment rate
    result = []
    for q, data in quarters.items():
        if data['total_orders'] > 0:
            data['avg_order_value'] = round(data['total_revenue'] / data['total_orders'], 2)
            data['fulfillment_rate'] = round((data['delivered_orders'] / data['total_orders']) * 100, 1)
        result.append(data)

    # Sort by quarter
    result.sort(key=lambda x: x['quarter'])
    return result

@app.get("/api/reports/monthly-trends")
def get_monthly_trends():
    """Get month-over-month trends"""
    months = {}

    for order in orders:
        order_date = order.get('order_date', '')
        if not order_date:
            continue

        # Extract month (format: YYYY-MM-DD)
        month = order_date[:7]  # Gets YYYY-MM

        if month not in months:
            months[month] = {
                'month': month,
                'order_count': 0,
                'revenue': 0,
                'delivered_count': 0
            }

        months[month]['order_count'] += 1
        months[month]['revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            months[month]['delivered_count'] += 1

    # Convert to list and sort
    result = list(months.values())
    result.sort(key=lambda x: x['month'])
    return result

@app.get("/api/restock/recommendations", response_model=RestockRecommendation)
def get_restock_recommendations(budget: float = Query(..., ge=0, le=MAX_RESTOCK_BUDGET, allow_inf_nan=False)):
    """Recommend forecast items to restock within the given budget"""
    return recommend_restock(budget)

@app.get("/api/restock-orders", response_model=List[RestockOrder])
def get_restock_orders():
    """Get submitted restock orders, newest first"""
    return list(reversed(restock_orders))

@app.post("/api/restock-orders", response_model=RestockOrder, status_code=201)
def create_restock_order(request: CreateRestockOrderRequest):
    """Submit a restock order"""
    if not request.items:
        raise HTTPException(status_code=400, detail="Restock order must contain at least one item")

    skus = [requested.sku for requested in request.items]
    if len(skus) != len(set(skus)):
        raise HTTPException(status_code=400, detail="Each SKU may appear only once per restock order")

    forecasts_by_sku = {f['item_sku']: f for f in demand_forecasts}
    items = []
    for requested in request.items:
        forecast = forecasts_by_sku.get(requested.sku)
        if not forecast:
            raise HTTPException(status_code=400, detail=f"Unknown SKU {requested.sku}")
        if requested.quantity <= 0:
            raise HTTPException(status_code=400, detail=f"Quantity for {requested.sku} must be positive")
        # Price from server-side data so the client can't submit its own unit costs.
        items.append(build_line_item(forecast, requested.quantity))

    total_cost = round(sum(line['line_total'] for line in items), 2)
    if total_cost > request.budget:
        raise HTTPException(
            status_code=400,
            detail=f"Order total {total_cost:.2f} exceeds budget {request.budget:.2f}"
        )

    # The order arrives complete, so it ships when its slowest item does.
    lead_time_days = max(line['lead_time_days'] for line in items)
    submitted = date.today()
    with restock_orders_lock:
        order_number = len(restock_orders) + 1
        order = {
            'id': str(order_number),
            'order_number': f"RST-{order_number:04d}",
            'items': items,
            'total_cost': total_cost,
            'budget': request.budget,
            'status': 'Submitted',
            'submitted_date': submitted.isoformat(),
            'lead_time_days': lead_time_days,
            'expected_delivery': (submitted + timedelta(days=lead_time_days)).isoformat(),
        }
        restock_orders.append(order)
    return order

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
