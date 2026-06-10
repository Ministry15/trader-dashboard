// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// ─── Minimal interfaces ───────────────────────────────────────────────────────

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function decimals() external view returns (uint8);
}

interface ICToken {
    function underlying() external view returns (address);
    function liquidateBorrow(address borrower, uint256 repayAmount, address cTokenCollateral) external returns (uint256);
    function redeem(uint256 redeemTokens) external returns (uint256);
    function balanceOf(address account) external view returns (uint256);
}

interface IAavePool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
}

// SwapRouter02 (no deadline field)
interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params) external returns (uint256 amountOut);

    struct ExactOutputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 amountOut;
        uint256 amountInMaximum;
        uint160 sqrtPriceLimitX96;
    }
    function exactOutputSingle(ExactOutputSingleParams calldata params) external returns (uint256 amountIn);
}

// ─── Contract ─────────────────────────────────────────────────────────────────

contract MoonwellLiquidatorBase {

    // ── Base mainnet addresses ─────────────────────────────────────────────────
    address public constant AAVE_POOL   = 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5;
    address public constant SWAP_ROUTER = 0x2626664c2603336E57B271c5C0b26F421741e481;
    address public constant USDC        = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;

    // ── State ─────────────────────────────────────────────────────────────────
    address public owner;

    // ── Flash loan callback payload ───────────────────────────────────────────
    struct LiqParams {
        address cDebt;           // cToken of the debt asset
        address cCollateral;     // cToken of the collateral asset
        address borrower;
        uint24  poolFee;         // Uniswap V3 fee tier for swap
    }

    // ── Modifiers ─────────────────────────────────────────────────────────────
    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    modifier onlyAavePool() {
        require(msg.sender == AAVE_POOL, "Not Aave Pool");
        _;
    }

    // ── Constructor ───────────────────────────────────────────────────────────
    constructor(address _owner) {
        require(_owner != address(0), "Zero address");
        owner = _owner;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // External entrypoint — called by the bot
    // ─────────────────────────────────────────────────────────────────────────

    function executeMoonwellLiquidation(
        address cDebt,
        address cCollateral,
        address borrower,
        uint256 repayAmount,
        uint24  poolFee
    ) external onlyOwner {
        address debtUnderlying = ICToken(cDebt).underlying();
        bytes memory params = abi.encode(LiqParams({
            cDebt:       cDebt,
            cCollateral: cCollateral,
            borrower:    borrower,
            poolFee:     poolFee
        }));
        IAavePool(AAVE_POOL).flashLoanSimple(
            address(this),
            debtUnderlying,
            repayAmount,
            params,
            0
        );
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Aave flash loan callback
    // ─────────────────────────────────────────────────────────────────────────

    function executeOperation(
        address asset,       // debt underlying (received from Aave)
        uint256 amount,      // repay amount
        uint256 premium,     // Aave flash fee (0.05%)
        address,             // initiator (ignored)
        bytes calldata params
    ) external onlyAavePool returns (bool) {
        LiqParams memory p = abi.decode(params, (LiqParams));

        // 1. Approve cDebt contract to pull our underlying tokens
        require(IERC20(asset).approve(p.cDebt, amount), "approve cDebt failed");

        // 2. Liquidate on Moonwell → receive seized cCollateral tokens
        uint256 err = ICToken(p.cDebt).liquidateBorrow(p.borrower, amount, p.cCollateral);
        require(err == 0, "liquidateBorrow failed");

        // 3. Redeem all seized cCollateral → underlying collateral
        uint256 cColBal = ICToken(p.cCollateral).balanceOf(address(this));
        require(cColBal > 0, "No cCollateral seized");
        err = ICToken(p.cCollateral).redeem(cColBal);
        require(err == 0, "redeem failed");

        // 4. Swap collateral → debt asset if tokens differ
        _swapIfNeeded(asset, amount + premium, p);

        // 5. Approve Aave to pull repayment
        require(IERC20(asset).approve(AAVE_POOL, amount + premium), "approve repay failed");

        return true;
    }

    // ── Internal swap helper ──────────────────────────────────────────────────
    function _swapIfNeeded(address asset, uint256 totalOwed, LiqParams memory p) internal {
        address colUnderlying = ICToken(p.cCollateral).underlying();
        if (colUnderlying == asset) return;

        uint256 colBal = IERC20(colUnderlying).balanceOf(address(this));
        require(colBal > 0, "No collateral after redeem");
        require(IERC20(colUnderlying).approve(SWAP_ROUTER, colBal), "approve router failed");

        if (asset == USDC) {
            // Sell ALL collateral → USDC; require at least totalOwed back
            ISwapRouter(SWAP_ROUTER).exactInputSingle(
                ISwapRouter.ExactInputSingleParams({
                    tokenIn:           colUnderlying,
                    tokenOut:          asset,
                    fee:               p.poolFee,
                    recipient:         address(this),
                    amountIn:          colBal,
                    amountOutMinimum:  totalOwed,
                    sqrtPriceLimitX96: 0
                })
            );
        } else {
            // Buy exactly totalOwed of debt asset, spend at most colBal collateral
            ISwapRouter(SWAP_ROUTER).exactOutputSingle(
                ISwapRouter.ExactOutputSingleParams({
                    tokenIn:           colUnderlying,
                    tokenOut:          asset,
                    fee:               p.poolFee,
                    recipient:         address(this),
                    amountOut:         totalOwed,
                    amountInMaximum:   colBal,
                    sqrtPriceLimitX96: 0
                })
            );
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Owner functions
    // ─────────────────────────────────────────────────────────────────────────

    function withdrawToken(address token) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        require(bal > 0, "Nothing to withdraw");
        IERC20(token).transfer(owner, bal);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "Zero address");
        owner = newOwner;
    }
}
