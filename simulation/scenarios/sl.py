"""
止损测试场景
测试止损单创建和触发流程
"""
from loguru import logger
from simulation.runner import SimulationRunner
from simulation.logger import EventType


async def scenario_sl(runner: SimulationRunner):
    """
    止损场景

    测试流程：
    1. 市价开多
    2. 创建止损单
    3. 验证止损单状态
    4. 移动价格触发止损
    5. 验证仓位关闭
    6. 验证余额减少（亏损）
    """
    logger.info("=" * 60)
    logger.info("开始测试：止损")
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

    # Step 2: 创建止损单
    sl_price = entry_price * 0.90  # 下跌 10%
    logger.info(f"\n[Step 2] 创建止损单 @ {sl_price}")

    runner.log.log(EventType.SL_CREATED, "创建止损单", {
        "symbol": "BTC/USDT:USDT",
        "price": sl_price,
    })

    sl_order = await runner.exchange.create_order(
        symbol="BTC/USDT:USDT",
        order_type="stop_loss",
        side="sell",
        amount=0.1,
        price=sl_price,
        params={"reduceOnly": True},
    )

    logger.info(f"止损单创建成功: {sl_order['id']}")

    # Step 3: 验证止损单状态
    logger.info("\n[Step 3] 验证止损单状态")
    open_orders = await runner.exchange.fetch_open_orders()

    runner.assertions.assert_order_exists(
        orders=open_orders,
        order_type="stop_loss",
        side="sell",
        status="pending",
    )

    # Step 4: 验证仓位存在
    logger.info("\n[Step 4] 验证仓位存在")
    await runner.position_manager.rest_refresh()
    positions = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_position_exists(
        positions=positions,
        symbol="BTC/USDT:USDT",
        side="long",
    )

    # Step 5: 移动价格触发止损
    logger.info(f"\n[Step 5] 移动价格到 {sl_price}")
    runner.log.log(EventType.PRICE_UPDATE, "价格移动", {
        "symbol": "BTC/USDT:USDT",
        "new_price": sl_price,
    })

    runner.exchange.update_price("BTC/USDT:USDT", sl_price)

    # Step 6: 验证仓位关闭
    logger.info("\n[Step 6] 验证仓位关闭")
    await runner.position_manager.rest_refresh()
    positions = runner.position_manager.get_snapshot_raw()

    runner.assertions.assert_position_not_exists(
        positions=positions,
        symbol="BTC/USDT:USDT",
    )

    # Step 7: 验证止损单已消失
    logger.info("\n[Step 7] 验证止损单已消失")
    open_orders = await runner.exchange.fetch_open_orders()

    runner.assertions.assert_order_not_exists(
        orders=open_orders,
        order_type="stop_loss",
        status="pending",
    )

    # Step 8: 验证余额减少（亏损）
    logger.info("\n[Step 8] 验证余额")
    balance = await runner.exchange.fetch_balance()
    free_balance = balance["USDT"]["free"]

    logger.info(f"可用余额: {free_balance} USDT")

    # 初始余额 10000，亏损约 5000 * 0.1 * 0.10 = 500
    runner.assertions.assert_balance_less_than(
        balance=balance,
        max_amount=9600,  # 应该小于初始余额
    )

    # 记录完成
    runner.log.log(EventType.SL_TRIGGERED, "止损触发完成", {"symbol": "BTC/USDT:USDT"})

    logger.info("\n" + "=" * 60)
    logger.info("止损测试完成")
    logger.info("=" * 60)
