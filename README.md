# Monitor definitivo — linhas 0.167 e 167.1

O monitor acompanha os ônibus na parada da Via L2 Sul/SAUS, Quadra 5,
próxima à OAB e ao Galois, no sentido do Guará.

Os dois Telegrams recebem alertas quando a estimativa entra nas faixas de
aproximadamente 30 e 15 minutos.

## Arquivos que devem ficar no repositório

- `monitor_onibus.py`
- `config.json`
- `requirements.txt`
- `.github/workflows/monitor.yml`

Os arquivos antigos de diagnóstico podem ser apagados depois.

## Secrets necessários

Em `Settings > Secrets and variables > Actions`:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_IDS`

O segundo deve conter os dois IDs separados por vírgula.

## Teste final

Em `Actions > Monitor de ônibus DF > Run workflow`, escolha `testar_fonte`.

O resultado deve mostrar:

- os itinerários das duas linhas;
- a parada OAB/Galois;
- conexão com a fonte em tempo real;
- ao menos um evento de posições, quando houver ônibus ativo.

## Funcionamento automático

O workflow inicia às 15h50, de segunda a sexta-feira, no fuso de Brasília.
O programa aguarda 16h e funciona até 21h.

Para mudar o intervalo, altere no `config.json`:

```json
"monitor_start": "16:00",
"monitor_end": "21:00"
```

## Teste prático

A opção `monitorar_agora` executa imediatamente por uma hora, sem considerar
o horário configurado. Ela pode disparar alertas reais se houver ônibus dentro
das faixas de 30 ou 15 minutos.

## Observação

A estimativa combina distância pelo itinerário, velocidade recebida, movimento
observado, quantidade de paradas restantes e fator de trânsito. É aproximada e
pode variar em razão do tráfego, das paradas e das atualizações do GPS.
