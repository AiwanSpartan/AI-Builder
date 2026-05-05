from pipeline import run_phased_builder
prompts=[
    'Build a weather API that returns temperature and condition via GET /weather',
    'Build an items API with POST /items and GET /items'
]
for p in prompts:
    print('=== PROMPT:',p)
    rc=run_phased_builder(p)
    print('RC',rc)
