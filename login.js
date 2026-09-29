// Run locally after putting EVM_WALLET_PRIVATE_KEY and STANDX_SIGN_KEY_HEX in .env.
// Never publish your wallet private key, JWT, or signing key.
require("dotenv").config();
const axios = require("axios");
const { ethers } = require("ethers");
const { ed25519 } = require("@noble/curves/ed25519.js");
const { base58 } = require("@scure/base");

async function login() {
    const privateKey = process.env.EVM_WALLET_PRIVATE_KEY;
    const signKeyHex = process.env.STANDX_SIGN_KEY_HEX;
    if (!privateKey || !/^(0x)?[a-f0-9]{64}$/i.test(privateKey) ||
        !signKeyHex || !/^[a-f0-9]{64}$/i.test(signKeyHex)) {
        throw new Error("Set EVM_WALLET_PRIVATE_KEY and STANDX_SIGN_KEY_HEX in .env first.");
    }
    const wallet = new ethers.Wallet(privateKey);
    const publicKey = ed25519.getPublicKey(Buffer.from(signKeyHex, "hex"));
    const requestId = base58.encode(publicKey);
    const prepare = await axios.post("https://api.standx.com/v1/offchain/prepare-signin?chain=bsc",
        { address: wallet.address, requestId });
    const signedData = prepare.data.signedData;
    const payload = JSON.parse(Buffer.from(signedData.split(".")[1], "base64url").toString());
    const signature = await wallet.signMessage(payload.message);
    const result = await axios.post("https://api.standx.com/v1/offchain/login?chain=bsc",
        { signature, signedData, expiresSeconds: 604800 });
    console.log("STANDX_TOKEN=" + result.data.token);
}
login().catch(error => { console.error(error.response?.data || error.message); process.exitCode = 1; });
