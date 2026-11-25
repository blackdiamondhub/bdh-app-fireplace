from bdh_sdk import PluginSpec, Setting, Tile
from bdh_sdk.plugins import BOOL, LIFECYCLE_ALWAYS, NETWORK_OUTBOUND, SECRET, STR

spec = PluginSpec(
    id="fireplace",
    app="fireplace",
    urls="fireplace.urls",
    status="fireplace.homepage.status",
    shortcuts="fireplace.homepage.shortcuts",
    run_shortcut="fireplace.homepage.run_shortcut",
    tiles=(
        Tile("Fireplace", "fireplace_index", "fireplace/hub/icon.svg", section="lodge",
             subtitle="Flames, heat, ember bed & top light", order=15,
             # Animated flames while any fireplace is on (status level ON).
             icon_on="fireplace/hub/icon-on.svg"),
    ),
    settings=(
        Setting("napoleon_email", "Napoleon email", type=STR, default="",
                help="The email you sign in to the Napoleon fireplace app with."),
        Setting("napoleon_password", "Napoleon password", type=SECRET),
        Setting("napoleon_europe", "European account", type=BOOL, default=False,
                help="Turn on if the account was made in the European Napoleon app."),
    ),
    api_version=2,
    # The Napoleon (Ayla) cloud.
    network=NETWORK_OUTBOUND,
    # The homepage shows whether a fireplace was left on and offers
    # "Fireplaces off", so the snapshot has to stay fresh.
    lifecycle=LIFECYCLE_ALWAYS,
    # SDK image smoke test: 58 MB RSS idle; pynapoleon + aiohttp add ~32 MB
    # once signed in, so ~90 MB in use. 128 leaves headroom (see README).
    memory_mb=128,
)
