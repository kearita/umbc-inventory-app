from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy.orm import Session
from database import get_db
from models import Order, OrderItem, Item
from schemas.orders import OrderCreate, OrderResponse, OrderItemUpdate
from models import Transaction, TransactionItem, User
from datetime import datetime, timedelta
from typing import List
from sqlalchemy import func, desc
from fastapi import Request
from routes.firebase_auth import get_current_user


router = APIRouter(prefix="/orders", tags=["Orders"])

# generate order
@router.post("/", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
def create_order(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """
    Generates a new order combining:
    - Items below restock threshold (suggested_quantity = threshold * 2)
    - Top 5 most withdrawn items in past 7 days (suggested_quantity = threshold or threshold * 1.5 if getting low)
    """
    # firebase_uid = request.state.user.uid
    # user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    new_order = Order(created_by_id=user.id)
    db.add(new_order)
    db.commit()
    db.refresh(new_order)
    # new_order.created_by = user

    order_items = []
    seven_days_ago = datetime.utcnow() - timedelta(days=7)

    # low stock
    low_stock_items = db.query(Item).filter(
        Item.deleted_at == None,
        Item.quantity < Item.restock_threshold
    ).all()

    for item in low_stock_items:
        suggested = item.restock_threshold * 2

        # Count total withdrawn for this item in the last 7 days
        withdrawn = db.query(func.sum(TransactionItem.quantity)).join(Transaction).filter(
            Transaction.transaction_type == "OUT",
            TransactionItem.item_id == item.id,
            Transaction.deleted_at == None,
            TransactionItem.deleted_at == None,
            Transaction.created_at >= seven_days_ago
        ).scalar() or 0

        print(f"[ORDER-GENERATOR] Added '{item.name}' (id={item.id}) due to LOW STOCK. Total withdrawn (7d): {withdrawn}. Qty suggested: {suggested}")

        order_items.append(OrderItem(
            order_id=new_order.id,
            item_id=item.id,
            suggested_quantity=suggested,
            final_quantity=suggested,
            supplier=None
        ))

    # popular items logic (past 7 days)
    popular_items = db.query(
        TransactionItem.item_id,
        func.sum(TransactionItem.quantity).label("total_withdrawn")
    ).join(Transaction).filter(
        Transaction.transaction_type == "OUT",
        Transaction.created_at >= seven_days_ago,
        Transaction.deleted_at == None,
        TransactionItem.deleted_at == None
    ).group_by(TransactionItem.item_id).order_by(desc("total_withdrawn")).limit(5).all()

    already_added_item_ids = {item.item_id for item in order_items}

    for item_id, total_withdrawn in popular_items:
        if item_id in already_added_item_ids:
            continue

        item = db.query(Item).filter(Item.id == item_id, Item.deleted_at == None).first()
        if not item:
            continue

        if item.quantity < item.restock_threshold * 1.5:
            suggested = item.restock_threshold
        else:
            suggested = item.restock_threshold // 2

        print(f"[ORDER-GENERATOR] Added '{item.name}' (id={item.id}) due to POPULARITY. Total withdrawn (7d): {total_withdrawn}. Qty suggested: {suggested}")

        order_items.append(OrderItem(
            order_id=new_order.id,
            item_id=item.id,
            suggested_quantity=suggested,
            final_quantity=suggested,
            supplier=None
        ))

    if not order_items:
        raise HTTPException(status_code=400, detail="All items are stocked and none are highly requested. No order needed.")

    db.add_all(order_items)
    db.commit()
    db.refresh(new_order)
    for order_item in new_order.order_items:
        order_item.item = db.query(Item).filter(Item.id == order_item.item_id).first()
        order_item.withdrawn_7d = db.query(func.sum(TransactionItem.quantity)).join(Transaction).filter(
            Transaction.transaction_type == "OUT",
            TransactionItem.item_id == order_item.item_id,
            Transaction.deleted_at == None,
            TransactionItem.deleted_at == None,
            Transaction.created_at >= seven_days_ago
        ).scalar() or 0

    return new_order

# get all active orders
@router.get("/", response_model=List[OrderResponse])
def get_orders(db: Session = Depends(get_db)):
    """
    Retrieves all orders that have not been soft deleted.
    """
    return db.query(Order).filter(Order.deleted_at == None).all()

# Get a single order by ID
@router.get("/{order_id}", response_model=OrderResponse)
def get_order(order_id: int, db: Session = Depends(get_db)):
    """
    Retrieves a single order by ID.
    """
    order = db.query(Order).filter(Order.id == order_id, Order.deleted_at == None).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

# soft delete an order
@router.delete("/{order_id}")
def delete_order(order_id: int, db: Session = Depends(get_db)):
    """
    Marks an order as deleted instead of fully removing it.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.deleted_at = datetime.utcnow()
    db.commit()
    
    return {"message": "Order soft deleted successfully"}

@router.post("/{order_id}/submit", response_model=OrderResponse)
def submit_order(order_id: int, db: Session = Depends(get_db)):
    """
    Finalizes a draft order:
    - Marks it as submitted
    - Adds inventory based on final_quantity
    """
    order = db.query(Order).filter(Order.id == order_id, Order.deleted_at == None).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.submitted:
        raise HTTPException(status_code=400, detail="Order has already been submitted")

    for order_item in order.order_items:
        item = db.query(Item).filter(Item.id == order_item.item_id, Item.deleted_at == None).first()
        if not item:
            raise HTTPException(status_code=404, detail=f"Item with id={order_item.item_id} not found")

        item.quantity += order_item.final_quantity
        print(f"[ORDER-SUBMIT] Restocked '{item.name}' (id={item.id}) with +{order_item.final_quantity}")

    order.submitted = True
    order.submitted_at = datetime.utcnow()
    db.commit()
    db.refresh(order)

    print(f"[ORDER-SUBMIT] Order id={order.id} submitted at {order.submitted_at}")
    return order


@router.put("/{order_id}/items")
def update_order_items(order_id: int, updated_items: List[OrderItemUpdate], db: Session = Depends(get_db)):
    """
    Updates final quantities for each item in a draft order
    Expects a list of { item_id: int, final_quantity: int }
    """
    order = db.query(Order).filter(Order.id == order_id, Order.deleted_at == None, Order.submitted == False).first()
    if not order:
        raise HTTPException(status_code=404, detail="Draft order not found")

    for item_data in updated_items:
        item_id = item_data.item_id
        final_quantity = item_data.final_quantity


        if item_id is None or final_quantity is None:
            continue  # Skip incomplete entries

        order_item = db.query(OrderItem).filter(
            OrderItem.order_id == order_id,
            OrderItem.item_id == item_id
        ).first()

        if order_item:
            order_item.final_quantity = final_quantity

    db.commit()
    return {"message": "Order item quantities updated successfully"}

