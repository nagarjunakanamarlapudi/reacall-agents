"""Static visual tokens for the quiet RecallOps command-center shell."""

CSS = """
<style>
  :root {
    --recallops-navy: #12263a;
    --recallops-blue: #1f5d8f;
    --recallops-sky: #e8f2fb;
    --recallops-orange: #a74408;
    --recallops-sand: #fff1e6;
    --recallops-border: #d7e0e8;
  }
  .block-container {max-width: 1320px; padding-top: 1.6rem;}
  [data-testid="stSidebar"] {border-right: 1px solid var(--recallops-border);}
  [data-testid="stMetric"] {
    background: #f7f9fb;
    border: 1px solid var(--recallops-border);
    border-radius: .75rem;
    padding: .7rem;
  }
  .source-official, .source-synthetic {
    display: inline-block;
    border-radius: 999px;
    font-weight: 700;
    padding: .12rem .55rem;
    margin: .1rem 0;
  }
  .source-official {background: var(--recallops-sky); color: #124a73;}
  .source-synthetic {background: var(--recallops-sand); color: #813200;}
  .lifecycle {
    padding: .75rem 1rem;
    background: #f7f9fb;
    border-left: 4px solid var(--recallops-blue);
    border-radius: .25rem;
  }
</style>
"""
