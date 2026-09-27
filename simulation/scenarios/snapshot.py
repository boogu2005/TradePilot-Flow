"""
Snapshot 场景 - 测试快照机制
"""
from loguru import logger
from ..logger import EventType


async def scenario_snapshot(runner):
    """
    快照场景

    测试流程：
    1. 初始空仓快照
    2. 市价开仓
    3. 验证快照更新
    4. 验证快照元数据
    5. 验证快照年龄
    """
    logger.info("=" * 60)
    logger.info("开始测试：Snapshot")
    logger.info("=" * 60)

    # Step 1: 初始空仓快照
    logger.info("\n[Step 1] 验证初始空仓快照")
    await runner.position_manager.rest_refresh()
    positions = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_equal(
        actual=len(positions),
        expected=0,
        description="初始应该没有仓位",
    )

    # Step 2: 验证快照元数据
    logger.info("\n[Step 2] 验证快照元数据")
    metadata = runner.position_manager.get_metadata()

    runner.assertions.assert_true(
        value=metadata.get("healthy", False),
        description="快照应该健康",
    )

    runner.assertions.assert_greater_than(
        actual=metadata.get("refresh_count", 0),
        expected=0,
        description="刷新次数应该大于0",
    )

    # Step 3: 市价开仓
    logger.info("\n[Step 3] 市价开仓 BTC/USDT")
    runner.log.log(EventType.ENTRY_STARTED, "市价开仓", {"symbol": "BTC/USDT:USDT"})

    await runner.exchange.create_order(
        symbol="BTC/USDT:USDT",
        order_type="market",
        side="buy",
        amount=0.1,
    )

    # Step 4: 验证快照更新
    logger.info("\n[Step 4] 验证快照更新")
    runner.log.log(EventType.SNAPSHOT_REFRESH, "快照刷新")

    await runner.position_manager.rest_refresh()
    positions = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_position_exists(
        positions=positions,
        symbol="BTC/USDT:USDT",
        side="long",
    )

    # Step 5: 验证快照年龄
    logger.info("\n[Step 5] 验证快照年龄")
    age = runner.position_manager.get_age()

    runner.assertions.assert_less_than(
        actual=age,
        expected=5.0,  # 5秒内
        description="快照年龄应该小于5秒",
    )

    # Step 6: 验证快照健康状态
    logger.info("\n[Step 6] 验证快照健康状态")
    is_healthy = runner.position_manager.is_healthy()

    runner.assertions.assert_true(
        value=is_healthy,
        description="快照应该保持健康状态",
    )

    # Step 7: 验证快照数据完整性
    logger.info("\n[Step 7] 验证快照数据完整性")
    position = runner.position_manager.get_position("BTC/USDT:USDT", "long")

    runner.assertions.assert_not_none(
        value=position,
        description="仓位数据应该存在",
    )

    if position:
        runner.assertions.assert_greater_than(
            actual=position.get("contracts", 0),
            expected=0,
            description="仓位数量应该大于0",
        )

        runner.assertions.assert_greater_than(
            actual=position.get("entryPrice", 0),
            expected=0,
            description="入场价格应该大于0",
        )

    runner.log.log(EventType.SNAPSHOT_REFRESH, "快照验证完成", {
        "position_count": len(positions),
        "healthy": is_healthy,
    })

    logger.info("\n" + "=" * 60)
    logger.info("Snapshot 测试完成")
    logger.info("=" * 60)
