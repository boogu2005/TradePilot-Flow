"""
追踪止损测试场景
测试追踪止损的激活和触发流程
"""
from loguru import logger
from simulation.runner import SimulationRunner
from simulation.logger import EventType


async def scenario_trailing(runner: SimulationRunner):
    """
    追踪止损场景

    测试流程：
    1. 市价开多
    2. 创建追踪止损（回调 5%）
    3. 价格上涨，追踪止损跟随
    4. 价格回调触发止损
    5. 验证仓位关闭
    """
    logger.info("=" * 60)
    logger.info("开始测试：追踪止损")
    logger.info("=" * 60)

    # Step 1: 市价开多
    logger.info("\n[Step 1] 市价开多 BTC/USDT")
    ticker = await runner.exchange.fetch_ticker("BTC/USDT:USDT")
    entry_price = ticker["last"]

    await runner.exchange.create_order(
        symbol="BTC/USDT:USDT",
        order_type="market",
        side="buy",
        amount=0.1,
    )

    logger.info(f"开仓价格: {entry_price}")

    # Step 2: 创建追踪止损
    trailing_percent = 5.0  # 回调 5%
    logger.info(f"\n[Step 2] 创建追踪止损 (回调 {trailing_percent}%)")

    runner.log.log(EventType.TRAILING_CREATED, "创建追踪止损", {
        "symbol": "BTC/USDT:USDT",
        "trailing_percent": trailing_percent,
    })

    trailing_order = await runner.exchange.create_order(
        symbol="BTC/USDT:USDT",
        order_type="trailing",
        side="sell",
        amount=0.1,
        params={
            "reduceOnly": True,
            "trailingPercent": trailing_percent,
        },
    )

    logger.info(f"追踪止损创建成功: {trailing_order['id']}")

    # Step 3: 验证追踪止损状态
    logger.info("\n[Step 3] 验证追踪止损状态")
    open_orders = await runner.exchange.fetch_open_orders()

    runner.assertions.assert_order_exists(
        orders=open_orders,
        order_type="trailing_stop",
        side="sell",
        status="pending",
    )

    # Step 4: 价格上涨，追踪止损跟随
    logger.info("\n[Step 4] 价格上涨 10%")
    new_price = entry_price * 1.10
    runner.log.log(EventType.PRICE_UPDATE, "价格上涨", {
        "symbol": "BTC/USDT:USDT",
        "new_price": new_price,
    })

    runner.exchange.update_price("BTC/USDT:USDT", new_price)

    # 验证仓位仍然存在
    await runner.position_manager.rest_refresh()
    positions = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_position_exists(
        positions=positions,
        symbol="BTC/USDT:USDT",
        side="long",
    )

    # Step 5: 价格回调触发止损
    logger.info(f"\n[Step 5] 价格回调到 {new_price * 0.94} (低于最高点 6%)")
    trigger_price = new_price * 0.94
    runner.log.log(EventType.PRICE_UPDATE, "价格回调", {
        "symbol": "BTC/USDT:USDT",
        "new_price": trigger_price,
    })

    runner.exchange.update_price("BTC/USDT:USDT", trigger_price)

    # Step 6: 验证仓位关闭
    logger.info("\n[Step 6] 验证仓位关闭")
    await runner.position_manager.rest_refresh()
    positions = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_position_not_exists(
        positions=positions,
        symbol="BTC/USDT:USDT",
    )

    # Step 7: 验证追踪止损已消失
    logger.info("\n[Step 7] 验证追踪止损已消失")
    open_orders = await runner.exchange.fetch_open_orders()

    runner.assertions.assert_order_not_exists(
        orders=open_orders,
        order_type="trailing_stop",
        status="pending",
    )

    # Step 8: 验证余额（应该有盈利）
    logger.info("\n[Step 8] 验证余额")
    balance = await runner.exchange.fetch_balance()
    free_balance = balance["USDT"]["free"]

    logger.info(f"可用余额: {free_balance} USDT")

    # 记录完成
    runner.log.log(EventType.TRAILING_TRIGGERED, "追踪止损触发完成", {"symbol": "BTC/USDT:USDT"})

    logger.info("\n" + "=" * 60)
    logger.info("追踪止损测试完成")
    logger.info("=" * 60)
