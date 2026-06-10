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

contract AaveLiquidatorV2Arbitrum {

    // ── Arbitrum One mainnet addresses ────────────────────────────────────────
    address public constant AAVE_POOL   = 0x794a61358D6845594F94dc1DB02A252b5b4814aD;
    address public constant AAVE_ORACLE = 0xb56c2F0B653B2e0b10C9b928C8580Ac5Df02C7C7;
    address public constant SWAP_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address public constant USDC        = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831; // native USDC

    // ── Constants ─────────────────────────────────────────────────────────────
    uint256 private constant SLIPPAGE_BPS    = 200;
    uint256 private constant BPS_BASE        = 10_000;
    uint256 private constant ORACLE_TO_USDC  = 100;

    // ── State ─────────────────────────────────────────────────────────────────
    address public owner;

    // ── Flash loan callback payload ───────────────────────────────────────────
    struct LiqParams {
        address collateralAsset;
        address borrower;
        uint24  poolFee;
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
            0
        );
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Aave flash loan callback
    // ─────────────────────────────────────────────────────────────────────────

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address,
        bytes calldata params
    ) external onlyAavePool returns (bool) {
        LiqParams memory p = abi.decode(params, (LiqParams));

        IERC20(asset).approve(AAVE_POOL, amount);

        IAavePool(AAVE_POOL).liquidationCall(
            p.collateralAsset,
            asset,
            p.borrower,
            amount,
            false
        );

        uint256 totalOwed = amount + premium;

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
                    amountInMaximum:   usdcAvail,
                    sqrtPriceLimitX96: 0
                })
            );
        }

        IERC20(asset).approve(AAVE_POOL, totalOwed);

        return true;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Internal helpers
    // ─────────────────────────────────────────────────────────────────────────

    function _usdcMinOut(address collateral, uint256 colBal) internal view returns (uint256) {
        uint256 oraclePrice  = IAaveOracle(AAVE_ORACLE).getAssetPrice(collateral);
        uint256 colDecimals  = uint256(IERC20(collateral).decimals());
        uint256 expectedUSDC = colBal * oraclePrice / (10 ** colDecimals * ORACLE_TO_USDC);
        return expectedUSDC * (BPS_BASE - SLIPPAGE_BPS) / BPS_BASE;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Owner functions
    // ─────────────────────────────────────────────────────────────────────────

    function withdraw() external onlyOwner {
        uint256 bal = IERC20(USDC).balanceOf(address(this));
        require(bal > 0, "Nothing to withdraw");
        IERC20(USDC).transfer(owner, bal);
    }

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
