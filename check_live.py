"""Quick verification of live OTel ingestion (used during setup/testing)."""
import db

con = db.connect()
print("otel prompts:", con.execute(
    "SELECT COUNT(*) FROM prompts WHERE source='otel'").fetchone()[0])
print("otel requests:", con.execute(
    "SELECT COUNT(*) FROM api_requests WHERE source='otel'").fetchone()[0])
for r in con.execute(
        """SELECT substr(prompt_id,1,8), model, input_tokens, output_tokens,
                  cache_read_tokens, round(cost_usd,5), query_source, agent_name
           FROM api_requests WHERE source='otel' ORDER BY ts"""):
    print("REQ:", r)
for r in con.execute(
        """SELECT substr(prompt_id,1,8), injected, substr(canonical_id,1,8),
                  substr(text,1,60) FROM prompts WHERE source='otel'"""):
    print("PROMPT:", r)
for r in con.execute(
        "SELECT tool_use_id, tool_name, source FROM tool_calls WHERE source='otel'"):
    print("TOOL:", r)
