"""
限价入场测试场景
测试限价挂单和成交流程
"""
from loguru import logger
from simulation.runner import SimulationRunner
from simulation.logger import EventType


async def scenario_limit_entry(runner: SimulationRunner):
    """
    限价入场场景

    测试流程：
    1. 限价挂单（低于市价）
    2. 验证挂单状态
    3. 移动价格触发成交
    4. 验证仓位创建
    """
    logger.info("=" * 60)
    logger.info("开始测试：限价入场")
    logger.info("=" * 60)

    # Step 1: 获取当前价格
    ticker = await runner.exchange.fetch_ticker("BTC/USDT:USDT")
    current_price = ticker["last"]
    limit_price = current_price * 0.95  # 低于市价 5%

    logger.info(f"\n[Step 1] 当前价格: {current_price}")
    logger.info(f"[Step 1] 限价挂单价格: {limit_price}")

    # Step 2: 限价挂单
    runner.log.log(EventType.ENTRY_STARTED, "限价挂单", {
        "symbol": "BTC/USDT:USDT",
        "side": "buy",
        "price": limit_price,
    })

    order = await runner.exchange.create_order(
        symbol="BTC/USDT:USDT",
        order_type="limit",
        side="buy",
        amount=0.1,
        price=limit_price,
    )

    logger.info(f"限价单创建成功: {order['id']}")
    logger.info(f"订单状态: {order['status']}")

    # Step 3: 验证挂单状态
    logger.info("\n[Step 2] 验证挂单状态")
    open_orders = await runner.exchange.fetch_open_orders()

    runner.assertions.assert_order_exists(
        orders=open_orders,
        order_type="limit",
        side="buy",
        status="pending",
    )

    # Step 4: 验证无仓位
    logger.info("\n[Step 3] 验证无仓位")
    await runner.position_manager.rest_refresh()
    positions = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_position_not_exists(
        positions=positions,
        symbol="BTC/USDT:USDT",
    )

    # Step 5: 移动价格触发成交
    logger.info(f"\n[Step 4] 移动价格到 {limit_price}")
    runner.log.log(EventType.PRICE_UPDATE, "价格移动", {
        "symbol": "BTC/USDT:USDT",
        "new_price": limit_price,
    })

    runner.exchange.update_price("BTC/USDT:USDT", limit_price)

    # Step 6: 验证成交
    logger.info("\n[Step 5] 验证成交")
    await runner.position_manager.rest_refresh()
    positions = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_position_exists(
        positions=positions,
        symbol="BTC/USDT:USDT",
        side="long",
    )

    runner.assertions.assert_position_size(
        positions=positions,
        symbol="BTC/USDT:USDT",
        expected_size=0.1,
    )

    # Step 7: 验证挂单已消失
    logger.info("\n[Step 6] 验证挂单已消失")
    open_orders = await runner.exchange.fetch_open_orders()

    runner.assertions.assert_order_not_exists(
        orders=open_orders,
        order_type="limit",
        status="pending",
    )

    # 记录完成
    runner.log.log(EventType.ENTRY_FILLED, "限价入场完成", {"symbol": "BTC/USDT:USDT"})

    logger.info("\n" + "=" * 60)
    logger.info("限价入场测试完成")
    logger.info("=" * 60)
