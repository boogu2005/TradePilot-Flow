"""
Restart 场景 - 测试重启恢复
"""
from loguru import logger
from ..logger import EventType


async def scenario_restart(runner):
    """
    重启场景

    测试流程：
    1. 市价开仓
    2. 创建 SL/TP
    3. 模拟系统重启（重置 PositionManager）
    4. 验证重启后恢复
    5. 验证仓位状态一致
    """
    logger.info("=" * 60)
    logger.info("开始测试：Restart")
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
    logger.info("\n[Step 2] 创建 SL/TP 订单")
    ticker = await runner.exchange.fetch_ticker("BTC/USDT:USDT")
    entry_price = ticker["last"]

    sl_price = entry_price * 0.95
    await runner.exchange.create_order(
        symbol="BTC/USDT:USDT",
        order_type="stop_loss",
        side="sell",
        amount=0.1,
        price=sl_price,
        params={"reduceOnly": True},
    )

    tp_price = entry_price * 1.10
    await runner.exchange.create_order(
        symbol="BTC/USDT:USDT",
        order_type="take_profit",
        side="sell",
        amount=0.1,
        price=tp_price,
        params={"reduceOnly": True},
    )

    # Step 3: 验证初始状态
    logger.info("\n[Step 3] 验证初始状态")
    await runner.position_manager.rest_refresh()
    positions_before = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_position_exists(
        positions=positions_before,
        symbol="BTC/USDT:USDT",
        side="long",
    )

    # Step 4: 模拟系统重启
    logger.info("\n[Step 4] 模拟系统重启")
    runner.log.log(EventType.SYSTEM_STOP, "系统停止")

    # 重置 PositionManager（模拟重启）
    await runner.position_manager.stop()
    runner.position_manager._snapshot = []
    runner.position_manager._snapshot_time = 0
    runner.position_manager._started = False

    # Step 5: 重启系统
    logger.info("\n[Step 5] 重启系统")
    runner.log.log(EventType.SYSTEM_START, "系统重启")

    await runner.position_manager.start()

    # Step 6: 验证重启后恢复
    logger.info("\n[Step 6] 验证重启后恢复")
    positions_after = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_position_exists(
        positions=positions_after,
        symbol="BTC/USDT:USDT",
        side="long",
    )

    # Step 7: 验证仓位状态一致
    logger.info("\n[Step 7] 验证仓位状态一致")
    position = runner.position_manager.get_position("BTC/USDT:USDT", "long")

    runner.assertions.assert_not_none(
        value=position,
        description="重启后仓位应该恢复",
    )

    if position:
        runner.assertions.assert_equal(
            actual=position["contracts"],
            expected=0.1,
            description="仓位数量应该一致",
        )

        runner.assertions.assert_equal(
            actual=position["entryPrice"],
            expected=entry_price,
            description="入场价格应该一致",
        )

    # Step 8: 验证订单状态
    logger.info("\n[Step 8] 验证订单状态")
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

    logger.info("\n" + "=" * 60)
    logger.info("Restart 测试完成")
    logger.info("=" * 60)
