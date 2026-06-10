// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../src/AaveLiquidatorV2Arbitrum.sol";

contract DeployArbitrum is Script {
    function run() external {
        uint256 deployerKey = vm.envUint("PRIVATE_KEY");
        address deployer    = vm.addr(deployerKey);

        vm.startBroadcast(deployerKey);
        AaveLiquidatorV2Arbitrum liq = new AaveLiquidatorV2Arbitrum(deployer);
        vm.stopBroadcast();

        console.log("AaveLiquidatorV2Arbitrum deployed:", address(liq));
        console.log("Owner:", deployer);
    }
}
