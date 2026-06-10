# BACKLOG — Trading Bot Infrastructure

## PRÉ-LIVE OBRIGATÓRIO
- [ ] Upgrade Alchemy Pay As You Go (obrigatório antes de ir live)
- [ ] Reescrever contrato flash loan Base com auto-swap USDC via Uniswap V3
- [ ] Desenvolver contrato flash loan com auto-swap para Polygon, Avalanche, Arbitrum
- [ ] Deploy flash loan Polygon (precisa MATIC)
- [ ] Deploy flash loan Avalanche
- [ ] Deploy flash loan Arbitrum
- [ ] WebSocket migration no bot Aave Base
- [ ] 48h DRY_RUN estável confirmado antes de ir live

## PRIORIDADE ALTA
- [ ] Auto-swap para USDC no contrato flash loan — lucro directo em stablecoin, zero risco de preço
- [ ] Reorganizar dashboard — muita informação, navegação a ficar complexa

## PRIORIDADE MÉDIA
- [ ] FileHandlers nos 9 bots Aave antigos — logs para ficheiro além de console
- [ ] Compound V3 Base — adicionar Comet USDT (0x3Afdc9BCA9213A35503b077a6072F3D0d5AB0840)

## PRÓXIMOS BOTS
- [ ] BSC Venus (lógica diferente, decisão após dados 48h)
- [ ] Compound V3 Polygon — já criado, avaliar após 48h
- [ ] Morpho Optimism — avaliar necessidade

## ORDEM DE IR LIVE
1. Base Aave (primeiro)
2. Polygon
3. Avalanche
4. Arbitrum
5. BSC Venus
6. Optimism
7. Scroll / Linea
8. Compound V3
9. Morpho

## ROADMAP — Melhorias de Edge (por ordem de impacto)

### FASE 1 — Esta semana
- [ ] WebSocket migration — substituir HTTP polling por WebSocket em todos os bots Aave (latência 30s → <1s)
- [ ] Contrato flash loan com auto-swap USDC via Uniswap V3 (Base) — lucro directo em stablecoin
- [ ] Gas dinâmico — priority fee dinâmica baseada na competição do bloco actual

### FASE 2 — Após ir live
- [ ] Capital próprio Polygon (~€200-500 MATIC) — eliminar flash loan, execução mais rápida
- [ ] Deploy contratos auto-swap Polygon, Avalanche, Arbitrum
- [ ] Multicall batching — verificar todas as posições numa única chamada (scan 30s → 2-3s)

### FASE 3 — Optimização avançada
- [ ] Mempool monitoring — detectar liquidações antes do bloco ser minado
- [ ] Node dedicado QuickNode (~$50/mês) — latência máxima quando houver lucro consistente
- [ ] ML/RL — prever quais posições têm maior probabilidade de chegar a HF < 1.0

### CUSTOS ESTIMADOS
- Fase 1: ~$5 (gas deploy contrato)
- Fase 2: ~€200-500 (capital MATIC, recuperável)
- Fase 3: ~$50/mês (node dedicado, só quando justificar)
