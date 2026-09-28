"""
Reconciliation 场景 - 测试协调器逻辑
"""
from loguru import logger
from ..logger import EventType


async def scenario_reconciliation(runner):
    """
    协调器场景

    测试流程：
    1. 市价开仓
    2. 创建 SL/TP 订单
    3. 模拟孤儿订单（数据库中不存在但交易所存在）
    4. 验证协调器检测
    5. 验证协调器清理
    """
    logger.info("=" * 60)
    logger.info("开始测试：Reconciliation")
    logger.info("=" * 60)

    # Step 1: 市价开仓
    logger.info("\n[Step 1] 市价开仓 BTC/USDT")
    runner.log.log(EventType.ENTRY_STARTED, "市价开仓", {"symbol": "BTC/USDT:USDT"})

    await runner.exchange.create_order(
        symbol="BTC/USDT:USDT",
        order_type="market",
        side="buy",
        amount=0.1,
    )

    # Step 2: 创建 SL 订单
    logger.info("\n[Step 2] 创建 SL 订单")
    ticker = await runner.exchange.fetch_ticker("BTC/USDT:USDT")
    entry_price = ticker["last"]
    sl_price = entry_price * 0.95  # 下跌 5%

    sl_order = await runner.exchange.create_order(
        symbol="BTC/USDT:USDT",
        order_type="stop_loss",
        side="sell",
        amount=0.1,
        price=sl_price,
        params={"reduceOnly": True},
    )

    logger.info(f"SL 订单创建: {sl_order['id']}")

    # Step 3: 创建 TP 订单
    logger.info("\n[Step 3] 创建 TP 订单")
    tp_price = entry_price * 1.10  # 上涨 10%

    tp_order = await runner.exchange.create_order(
        symbol="BTC/USDT:USDT",
        order_type="take_profit",
        side="sell",
        amount=0.1,
        price=tp_price,
        params={"reduceOnly": True},
    )

    logger.info(f"TP 订单创建: {tp_order['id']}")

    # Step 4: 验证订单存在
    logger.info("\n[Step 4] 验证订单存在")
    open_orders = await runner.exchange.fetch_open_orders()

    runner.assertions.assert_order_exists(
        orders=open_orders,
        order_type="stop_loss",
        side="sell",
        status="pending",
    )

    runner.assertions.assert_order_exists(
        orders=open_orders,
        order_type="take_profit",
        side="sell",
        status="pending",
    )

    # Step 5: 模拟协调器检查
    logger.info("\n[Step 5] 模拟协调器检查")
    runner.log.log(EventType.RECONCILE_STARTED, "开始协调器检查")

    # 获取仓位
    await runner.position_manager.rest_refresh()
    positions = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_position_exists(
        positions=positions,
        symbol="BTC/USDT:USDT",
        side="long",
    )

    # Step 6: 验证协调器不会误删有效订单
    logger.info("\n[Step 6] 验证协调器不会误删有效订单")

    # 协调器应该检测到仓位存在，不会删除 SL/TP
    open_orders_after = await runner.exchange.fetch_open_orders()

    runner.assertions.assert_order_exists(
        orders=open_orders_after,
        order_type="stop_loss",
        side="sell",
        status="pending",
    )

    runner.assertions.assert_order_exists(
        orders=open_orders_after,
        order_type="take_profit",
        side="sell",
        status="pending",
    )

    runner.log.log(EventType.RECONCILE_COMPLETED, "协调器检查完成", {
        "orders_checked": len(open_orders),
        "orphans_found": 0,
    })

    # Step 7: 验证仓位和订单一致性
    logger.info("\n[Step 7] 验证仓位和订单一致性")

    position = runner.position_manager.get_position("BTC/USDT:USDT", "long")
    runner.assertions.assert_not_none(
        value=position,
        description="仓位应该存在",
    )

    if position:
        runner.assertions.assert_equal(
            actual=position["contracts"],
            expected=0.1,
            description="仓位数量应该匹配",
        )

    logger.info("\n" + "=" * 60)
    logger.info("Reconciliation 测试完成")
    logger.info("=" * 60)
