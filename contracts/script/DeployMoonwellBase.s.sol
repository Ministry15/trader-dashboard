// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../src/MoonwellLiquidatorBase.sol";

contract DeployMoonwellBase is Script {
    function run() external {
        uint256 deployerKey = vm.envUint("PRIVATE_KEY");
        address deployer    = vm.addr(deployerKey);

        vm.startBroadcast(deployerKey);
        MoonwellLiquidatorBase liq = new MoonwellLiquidatorBase(deployer);
        vm.stopBroadcast();

        console.log("MoonwellLiquidatorBase deployed:", address(liq));
        console.log("Owner:", deployer);
    }
}
