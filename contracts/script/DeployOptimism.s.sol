// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../src/AaveLiquidatorV2Optimism.sol";

contract DeployOptimism is Script {
    function run() external {
        uint256 deployerKey = vm.envUint("PRIVATE_KEY");
        address deployer    = vm.addr(deployerKey);

        vm.startBroadcast(deployerKey);
        AaveLiquidatorV2Optimism liq = new AaveLiquidatorV2Optimism(deployer);
        vm.stopBroadcast();

        console.log("AaveLiquidatorV2Optimism deployed:", address(liq));
        console.log("Owner:", deployer);
    }
}
