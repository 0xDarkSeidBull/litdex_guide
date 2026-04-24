# LitDeX Decentralized Exchange on LitVM LiteForge
<img width="1916" height="913" alt="image" src="https://github.com/user-attachments/assets/73359916-b95c-4e6e-b5ca-d22fe8cbc2de" />

>community-built DEX on LitVM : Swap, Pool, and Deploy smart contracts directly from your browser.

🔗 **Live App:** [litdex.test-hub.xyz](https://litdex.test-hub.xyz)  
🔗 **Explorer:** [liteforge.explorer.caldera.xyz](https://liteforge.explorer.caldera.xyz)

---

## 🚀 Features

### 🔄 Swap
- Swap any token pair on LitVM LiteForge testnet
- Powered by UniswapV2-style AMM (0.3% fee)
- Supported tokens: zkLTC, USDC, USDT, WETH, WBTC, YURI, CHAWLEE, LITO, LITVM and more
- Real-time price impact and slippage display

### 💧 Pool (Liquidity)
- Add/Remove liquidity to any token pair
- Earn 0.3% fees on every swap proportional to your share
- Receive LP tokens representing your pool share
- View your active positions

### 🏗️ Deploy Contracts
Deploy smart contracts directly from the UI : no Remix, no coding required.

| Template | Description |
|---|---|
| **ERC20 Token** | Custom fungible token : name, symbol, supply, decimals, mintable, burnable |
| **NFT (ERC721)** | NFT collection with max supply and mint price |
| **Staking Contract** | Stake tokens and earn rewards at custom rate and lock period |
| **Vesting Contract** | Token vesting with cliff period and duration |
| **Token Factory** | Deploy a factory contract with custom deploy fee and owner |

---

## ⚙️ Network Configuration

| Field | Value |
|---|---|
| Network Name | LitVM LiteForge |
| RPC URL | `https://liteforge.rpc.caldera.xyz/http` |
| Chain ID | `4441` |
| Symbol | `zkLTC` |
| Explorer | `https://liteforge.explorer.caldera.xyz` |

---

## 🔑 Contract Addresses

| Contract | Address |
|---|---|
| Router | `0xFa1f665C6ee5167f78454d85bc56D263D5da4576` |
| WZKLTC | `0x60A84eBC3483fEFB251B76Aea5B8458026Ef4bea` |

---

## 🛠️ Tech Stack

- **Frontend:** React + TanStack Router + Tailwind CSS
- **Blockchain:** ethers.js v6
- **AMM:** UniswapV2 fork (LitVMFactory + LitVMRouter)
- **Deployment:** Vercel
- **Network:** LitVM LiteForge Testnet (Chain ID: 4441)

---

## 🧑‍💻 Local Development

```bash
git clone https://github.com/0xDarkSeidBull/litdex
cd litdex
npm install
npm run dev
```

---

## ⚠️ Disclaimer

This is a **testnet application** built on LitVM LiteForge. Tokens have no real value. Use for testing and development purposes only.

---

Built with 🔥 by [@0xDarkSeidBull](https://twitter.com/0xDarkSeidBull) for the LitVM community.
