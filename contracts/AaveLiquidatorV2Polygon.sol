// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// ─── Minimal interfaces ───────────────────────────────────────────────────────

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function decimals() external view returns (uint8);
}

interface IAavePool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;

    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
}

interface IAaveOracle {
    // Returns USD price of asset with 8 decimal places (e.g. 3000_00000000 for $3000)
    function getAssetPrice(address asset) external view returns (uint256);
}

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

    struct ExactOutputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 amountOut;
        uint256 amountInMaximum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params)
        external returns (uint256 amountOut);

    function exactOutputSingle(ExactOutputSingleParams calldata params)
        external returns (uint256 amountIn);
}

// ─── Contract ─────────────────────────────────────────────────────────────────

contract AaveLiquidatorV2 {

    // ── Polygon mainnet addresses ─────────────────────────────────────────────
    address public constant AAVE_POOL   = 0x794a61358D6845594F94dc1DB02A252b5b4814aD;
    address public constant AAVE_ORACLE = 0xb023e699F5a33916Ea823A16485e259257cA8Bd1;
    address public constant SWAP_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address public constant USDC        = 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174;

    // ── Constants ─────────────────────────────────────────────────────────────
    uint256 private constant SLIPPAGE_BPS = 200;    // 2% max slippage on collateral → USDC swap
    uint256 private constant BPS_BASE     = 10_000;
    // Oracle has 8 decimals; USDC has 6 → conversion factor 10^(8-6) = 100
    uint256 private constant ORACLE_TO_USDC = 100;

    // ── State ─────────────────────────────────────────────────────────────────
    address public owner;

    // ── Flash loan callback payload ───────────────────────────────────────────
    struct LiqParams {
        address collateralAsset;
        address borrower;
        uint24  poolFee;   // 500 = 0.05%, 3000 = 0.3%, 10000 = 1%
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
    // External entrypoint
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @notice Flash-loan debtAmount of debtAsset, liquidate borrower on Aave V3,
     *         swap seized collateral to USDC, repay flash loan.
     *         Profit in USDC accumulates in this contract; call withdraw() to collect.
     *
     * @param debtAsset       Token the borrower owes (flash-loaned from Aave).
     * @param collateralAsset Token seized as liquidation bonus.
     * @param borrower        Address of the under-collateralised position.
     * @param debtAmount      Exact debtAsset amount to cover (in token units).
     * @param poolFee         Uniswap V3 pool fee tier (500, 3000 or 10000).
     */
    function executeFlashLiquidation(
        address debtAsset,
        address collateralAsset,
        address borrower,
        uint256 debtAmount,
        uint24  poolFee
    ) external onlyOwner {
        bytes memory params = abi.encode(
            LiqParams({ collateralAsset: collateralAsset, borrower: borrower, poolFee: poolFee })
        );
        IAavePool(AAVE_POOL).flashLoanSimple(
            address(this),
            debtAsset,
            debtAmount,
            params,
            0 // referralCode
        );
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Aave flash loan callback
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @notice Invoked by Aave Pool after transferring flash-loaned tokens.
     *         Must leave (amount + premium) of `asset` approved for the Pool to pull back.
     *
     * Flow:
     *   1. Approve Pool for liquidationCall
     *   2. liquidationCall → seize collateral
     *   3. If collateral ≠ USDC → exactInputSingle (collateral → USDC)
     *   4. If debtAsset ≠ USDC → exactOutputSingle (USDC → debtAsset, exact repayment)
     *   5. Approve Pool for flash loan repayment
     *   Remaining USDC is profit.
     */
    function executeOperation(
        address asset,    // debtAsset (flash-loaned)
        uint256 amount,   // flash loan principal
        uint256 premium,  // Aave fee (0.05%)
        address,          // initiator — not used; caller verified by onlyAavePool
        bytes calldata params
    ) external onlyAavePool returns (bool) {
        LiqParams memory p = abi.decode(params, (LiqParams));

        // ── 1. Approve Pool to pull debt repayment during liquidationCall ──────
        IERC20(asset).approve(AAVE_POOL, amount);

        // ── 2. Liquidate — Pool takes `amount` of debtAsset, sends collateral ──
        IAavePool(AAVE_POOL).liquidationCall(
            p.collateralAsset,
            asset,
            p.borrower,
            amount,
            false // receiveAToken = false → receive underlying, not aToken
        );

        uint256 totalOwed = amount + premium;

        // ── 3. Swap collateral → USDC (skip if collateral is already USDC) ────
        uint256 colBal = IERC20(p.collateralAsset).balanceOf(address(this));
        if (p.collateralAsset != USDC && colBal > 0) {
            uint256 minOut = _usdcMinOut(p.collateralAsset, colBal);
            IERC20(p.collateralAsset).approve(SWAP_ROUTER, colBal);
            ISwapRouter(SWAP_ROUTER).exactInputSingle(
                ISwapRouter.ExactInputSingleParams({
                    tokenIn:           p.collateralAsset,
                    tokenOut:          USDC,
                    fee:               p.poolFee,
                    recipient:         address(this),
                    amountIn:          colBal,
                    amountOutMinimum:  minOut,
                    sqrtPriceLimitX96: 0
                })
            );
        }

        // ── 4. If debtAsset ≠ USDC, buy back exactly totalOwed of debtAsset ───
        //    exactOutputSingle pulls only what's needed; leftover USDC is profit.
        if (asset != USDC) {
            uint256 usdcAvail = IERC20(USDC).balanceOf(address(this));
            IERC20(USDC).approve(SWAP_ROUTER, usdcAvail);
            ISwapRouter(SWAP_ROUTER).exactOutputSingle(
                ISwapRouter.ExactOutputSingleParams({
                    tokenIn:           USDC,
                    tokenOut:          asset,
                    fee:               p.poolFee,
                    recipient:         address(this),
                    amountOut:         totalOwed,
                    amountInMaximum:   usdcAvail, // revert if not enough USDC (unprofitable)
                    sqrtPriceLimitX96: 0
                })
            );
        }

        // ── 5. Approve Aave Pool to pull flash loan repayment ─────────────────
        IERC20(asset).approve(AAVE_POOL, totalOwed);

        // Profit: USDC balance remaining in this contract.
        return true;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Internal helpers
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @dev Minimum USDC output for `colBal` units of `collateral`, with 2% slippage.
     *
     *      expectedUSDC (6 dec) = colBal * oraclePrice / (10^colDecimals * ORACLE_TO_USDC)
     */
    function _usdcMinOut(address collateral, uint256 colBal) internal view returns (uint256) {
        uint256 oraclePrice  = IAaveOracle(AAVE_ORACLE).getAssetPrice(collateral);
        uint256 colDecimals  = uint256(IERC20(collateral).decimals());
        uint256 expectedUSDC = colBal * oraclePrice / (10 ** colDecimals * ORACLE_TO_USDC);
        return expectedUSDC * (BPS_BASE - SLIPPAGE_BPS) / BPS_BASE;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Owner functions
    // ─────────────────────────────────────────────────────────────────────────

    /// @notice Withdraw accumulated USDC profit to owner.
    function withdraw() external onlyOwner {
        uint256 bal = IERC20(USDC).balanceOf(address(this));
        require(bal > 0, "Nothing to withdraw");
        IERC20(USDC).transfer(owner, bal);
    }

    /// @notice Withdraw any ERC20 token (for dust or unexpected receipts).
    function withdrawToken(address token) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        require(bal > 0, "Nothing to withdraw");
        IERC20(token).transfer(owner, bal);
    }

    /// @notice Transfer contract ownership.
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "Zero address");
        owner = newOwner;
    }
}
