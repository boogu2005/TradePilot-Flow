"""
Watchdog 场景 - 测试看门狗机制
"""
from loguru import logger
from ..logger import EventType


async def scenario_watchdog(runner):
    """
    Watchdog 场景

    测试流程：
    1. 市价开仓
    2. 模拟 WS 断线
    3. 验证 Watchdog 检测
    4. 验证 REST Fallback 恢复
    5. 验证仓位状态正确
    """
    logger.info("=" * 60)
    logger.info("开始测试：Watchdog")
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

    # Step 2: 验证仓位创建
    logger.info("\n[Step 2] 验证仓位创建")
    await runner.position_manager.rest_refresh()
    positions = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_position_exists(
        positions=positions,
        symbol="BTC/USDT:USDT",
        side="long",
    )

    # Step 3: 模拟 WS 断线
    logger.info("\n[Step 3] 模拟 WS 断线")
    runner.log.log(EventType.WS_DISCONNECTED, "WS 连接断开", {"reason": "network_error"})

    # 标记 PositionManager 为不健康
    runner.position_manager._healthy = False
    runner.position_manager._ws_connected = False

    # Step 4: 验证 Watchdog 检测
    logger.info("\n[Step 4] 验证 Watchdog 检测")
    is_healthy = runner.position_manager.is_healthy()

    runner.assertions.assert_false(
        value=is_healthy,
        description="PositionManager 应该检测到不健康状态",
    )

    # Step 5: 触发 REST Fallback
    logger.info("\n[Step 5] 触发 REST Fallback")
    runner.log.log(EventType.CUSTOM, "启动 REST Fallback 恢复")

    # 执行 REST 刷新
    success = await runner.position_manager.rest_refresh()

    runner.assertions.assert_true(
        value=success,
        description="REST 刷新应该成功",
    )

    # Step 6: 验证恢复后状态
    logger.info("\n[Step 6] 验证恢复后状态")
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

    # Step 7: 验证健康状态恢复
    logger.info("\n[Step 7] 验证健康状态恢复")
    runner.position_manager._ws_connected = True
    is_healthy = runner.position_manager.is_healthy()

    runner.assertions.assert_true(
        value=is_healthy,
        description="PositionManager 应该恢复到健康状态",
    )

    runner.log.log(EventType.WS_CONNECTED, "WS 连接恢复", {"method": "rest_fallback"})

    logger.info("\n" + "=" * 60)
    logger.info("Watchdog 测试完成")
    logger.info("=" * 60)
