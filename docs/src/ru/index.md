# llmbroker

Маршрутизатор LLM-запросов над пулом endpoint-ов с автоматическим round-robin и
охлаждением при 429/503. Никаких тяжёлых зависимостей — только stdlib в ядре.

## Быстрый старт

Создайте файл `llms.toml` со списком LLM endpoint-ов (можно взять
[готовый пресет](https://github.com/andgineer/llmbroker/blob/main/presets/freetier.toml)
из репозитория):

```toml
[[llms]]
name        = "groq-llama"
base_url    = "https://api.groq.com/openai/v1"
model       = "llama-3.3-70b-versatile"
api_key_ref = "GROQ_API_KEY"

[[llms]]
name        = "groq-gemma"
base_url    = "https://api.groq.com/openai/v1"
model       = "gemma2-9b-it"
api_key_ref = "GROQ_API_KEY"
```

`api_key_ref` — имя переменной окружения с ключом. Секрет никогда не попадает в файл.

Установите ключи и вызовите брокер:

```python
import llmbroker

llms = llmbroker.Broker(registry=llmbroker.Registry("llms.toml"))
print(llms.ask("Привет, как дела?").text)
```

Если один endpoint вернул 429, брокер охладит его и переключится на следующий.
Вызывающий код не видит ошибку лимита, пока хотя бы один endpoint доступен.

## Как это работает

- Каждому LLM в пуле соответствует один слот очереди: не более одного активного
  запроса к endpoint-у одновременно.
- На 429/503 брокер ставит endpoint на паузу (`cooldown`) и повторно добавляет его
  в очередь после задержки (`Retry-After` из заголовка или 60 с по умолчанию).
- `ask(prompt)` — обёртка над `chat([{"role": "user", "content": prompt}])`.
- Если все endpoint-ы на паузе и истёк `wait` — `NoLLMAvailableError`.
- Если endpoint попробован и вернул ошибку — `AllLLMsFailedError`.
