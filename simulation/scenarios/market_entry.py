"""
市价入场测试场景
测试完整的市价开仓流程
"""
from loguru import logger
from simulation.runner import SimulationRunner
from simulation.logger import EventType


async def scenario_market_entry(runner: SimulationRunner):
    """
    市价入场场景

    测试流程：
    1. 市价开多 BTC/USDT
    2. 验证仓位创建
    3. 验证余额扣减
    4. 验证订单状态
    """
    logger.info("=" * 60)
    logger.info("开始测试：市价入场")
    logger.info("=" * 60)

    # 记录开始
    runner.log.log(EventType.CUSTOM, "市价入场测试开始")

    # Step 1: 市价开多
    logger.info("\n[Step 1] 市价开多 BTC/USDT")
    runner.log.log(EventType.ENTRY_STARTED, "市价开多", {"symbol": "BTC/USDT:USDT", "side": "long"})

    order = await runner.exchange.create_order(
        symbol="BTC/USDT:USDT",
        order_type="market",
        side="buy",
        amount=0.1,
    )

    logger.info(f"订单创建成功: {order['id']}")
    logger.info(f"订单状态: {order['status']}")

    # Step 2: 验证仓位
    logger.info("\n[Step 2] 验证仓位")
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

    # Step 3: 验证余额
    logger.info("\n[Step 3] 验证余额")
    balance = await runner.exchange.fetch_balance()
    free_balance = balance["USDT"]["free"]

    logger.info(f"可用余额: {free_balance} USDT")

    # 初始余额 10000，开仓 0.1 BTC @ ~100000，应该扣减约 10000 (100000 * 0.1 / 10 leverage = 1000 margin)
    # 实际可用余额约 9000
    runner.assertions.assert_balance_less_than(
        balance=balance,
        max_amount=9500,  # 应该小于初始余额
    )

    # Step 4: 验证订单状态
    logger.info("\n[Step 4] 验证订单状态")
    open_orders = await runner.exchange.fetch_open_orders()

    # 市价单应该立即成交，不应该有挂单
    runner.assertions.assert_order_not_exists(
        orders=open_orders,
        order_type="market",
        status="pending",
    )

    # 记录完成
    runner.log.log(EventType.ENTRY_FILLED, "市价开多完成", {"symbol": "BTC/USDT:USDT"})

    logger.info("\n" + "=" * 60)
    logger.info("市价入场测试完成")
    logger.info("=" * 60)
