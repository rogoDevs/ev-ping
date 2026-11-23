import json
import os
from datetime import datetime, timezone
from pathlib import Path
import requests

API_ROOT = "https://www.bungie.net/Platform"
BUNGIE_WEB = "https://www.bungie.net"
EVERVERSE_HASH = "3361454721"

CURRENCIES = {
    2817410917: "Bright Dust",
    3147280338: "Silver",
}
CLASS_NAMES = {0: "Titan", 1: "Hunter", 2: "Warlock", 3: "All Classes"}

CACHE_FILE = Path("manifest_cache.json")
OUTPUT_FILE = Path("docs/eververse.json")


def load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {"items": {}, "vendor": {}}


def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def refresh_bungie_tokens():
    resp = requests.post(
        f"{API_ROOT}/App/OAuth/token/",
        data={
            "grant_type": "refresh_token",
            "refresh_token": os.environ["BUNGIE_REFRESH_TOKEN"],
            "client_id": os.environ["BUNGIE_CLIENT_ID"],
            "client_secret": os.environ["BUNGIE_CLIENT_SECRET"],
        },
        timeout=25,
    )
    resp.raise_for_status()
    return resp.json()


def get_manifest_entity(entity_type, entity_hash, headers, cache):
    bucket = "vendor" if entity_type == "DestinyVendorDefinition" else "items"
    key = str(entity_hash)
    if key in cache[bucket]:
        return cache[bucket][key]

    resp = requests.get(
        f"{API_ROOT}/Destiny2/Manifest/{entity_type}/{key}/",
        headers=headers,
        timeout=20,
    )
    if resp.status_code != 200:
        return {}

    payload = resp.json()
    if payload.get("ErrorCode") != 1:
        return {}

    raw = payload["Response"]
    if bucket == "vendor":
        cleaned = {
            "failureStrings": raw.get("failureStrings", []),
            "displayCategories": [
                c.get("displayProperties", {}).get("name", "Store")
                for c in raw.get("displayCategories", [])
            ],
        }
    else:
        display = raw.get("displayProperties", {})
        icon = display.get("icon", "")
        cleaned = {
            "name": display.get("name") or "Classified Item",
            "description": display.get("description", ""),
            "icon": f"{BUNGIE_WEB}{icon}" if icon.startswith("/") else icon,
            "itemType": raw.get("itemTypeDisplayName") or "Eververse Item",
            "classType": CLASS_NAMES.get(raw.get("classType", 3), "All Classes"),
        }

    cache[bucket][key] = cleaned
    return cleaned


def main():
    tokens = refresh_bungie_tokens()
    access_token = tokens["access_token"]
    new_refresh_token = tokens.get("refresh_token", "")

    # Expose rotated refresh token to GitHub Actions workflow step
    github_env = os.environ.get("GITHUB_ENV")
    if github_env and new_refresh_token:
        with open(github_env, "a", encoding="utf-8") as env_file:
            env_file.write(f"NEW_REFRESH_TOKEN={new_refresh_token}\n")

    headers = {
        "X-API-Key": os.environ["BUNGIE_API_KEY"],
        "Authorization": f"Bearer {access_token}",
    }

    cache = load_cache()
    vendor_def = get_manifest_entity("DestinyVendorDefinition", EVERVERSE_HASH, headers, cache)
    failure_strings = vendor_def.get("failureStrings", [])
    display_categories = vendor_def.get("displayCategories", [])

    membership_type = os.environ["DESTINY_MEMBERSHIP_TYPE"]
    membership_id = os.environ["DESTINY_MEMBERSHIP_ID"]
    character_ids = [
        c.strip() for c in os.environ["DESTINY_CHARACTER_IDS"].split(",") if c.strip()
    ]

    merged_items = {}

    for char_id in character_ids:
        url = (
            f"{API_ROOT}/Destiny2/{membership_type}/Profile/{membership_id}"
            f"/Character/{char_id}/Vendors/{EVERVERSE_HASH}/"
        )
        resp = requests.get(
            url, headers=headers, params={"components": "400,401,402"}, timeout=25
        )
        resp.raise_for_status()
        data = resp.json()["Response"]

        # Map each sale index to its Eververse tab/category name
        sale_to_category = {}
        categories = data.get("categories", {}).get("data", {}).get("categories", [])
        for cat in categories:
            cat_idx = cat.get("displayCategoryIndex", 0)
            cat_name = (
                display_categories[cat_idx]
                if 0 <= cat_idx < len(display_categories) and display_categories[cat_idx]
                else "Featured"
            )
            for item_idx in cat.get("itemIndexes", []):
                sale_to_category[str(item_idx)] = cat_name

        sales = data.get("sales", {}).get("data", {})
        for sale_idx, sale in sales.items():
            item_hash = sale["itemHash"]
            item_info = get_manifest_entity(
                "DestinyInventoryItemDefinition", item_hash, headers, cache
            )

            # Determine currency & cost
            costs = sale.get("costs", [])
            if costs:
                cost_hash = costs[0]["itemHash"]
                cost_qty = costs[0]["quantity"]
                currency = CURRENCIES.get(cost_hash)
                if not currency:
                    curr_def = get_manifest_entity(
                        "DestinyInventoryItemDefinition", cost_hash, headers, cache
                    )
                    currency = curr_def.get("name", "Other")
            else:
                cost_qty = 0
                currency = "Free"

            # Check ownership via VendorItemStatus bit 4096 (AlreadyOwned) or failureStrings
            sale_status = sale.get("saleStatus", 0)
            reasons = [
                failure_strings[i]
                for i in sale.get("failureIndexes", [])
                if 0 <= i < len(failure_strings)
            ]
            owned = bool(sale_status & 4096) or any("already" in r.lower() for r in reasons)

            category_name = sale_to_category.get(str(sale_idx), "Featured")
            dedup_key = f"{item_hash}_{currency}_{cost_qty}"

            if dedup_key not in merged_items:
                merged_items[dedup_key] = {
                    "hash": item_hash,
                    "name": item_info.get("name", "Unknown Item"),
                    "itemType": item_info.get("itemType", "Eververse Item"),
                    "classType": item_info.get("classType", "All Classes"),
                    "icon": item_info.get("icon", ""),
                    "description": item_info.get("description", ""),
                    "category": category_name,
                    "currency": currency,
                    "cost": cost_qty,
                    "owned": owned,
                }
            elif owned:
                merged_items[dedup_key]["owned"] = True

    save_cache(cache)

    items_list = sorted(
        merged_items.values(),
        key=lambda x: (x["currency"] != "Bright Dust", x["owned"], x["category"], x["name"]),
    )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "items": items_list,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Send push notification via ntfy
    ntfy_topic = os.environ["NTFY_TOPIC"].strip()
    pages_url = "https://rogodevs.github.io/ev-ping/"
    unowned_bd = [
        i for i in items_list if i["currency"] == "Bright Dust" and not i["owned"]
    ]

    if unowned_bd:
        preview = "\n".join(f"• {i['name']} ({i['cost']} BD)" for i in unowned_bd[:5])
        if len(unowned_bd) > 5:
            preview += f"\n+ {len(unowned_bd) - 5} more..."
        title = f"Eververse: {len(unowned_bd)} Unowned Bright Dust Items"
    else:
        preview = "No unowned Bright Dust items today. Tap to view full store."
        title = "Eververse Daily Reset"

    requests.post(
        f"https://ntfy.sh/{ntfy_topic}",
        data=preview.encode("utf-8"),
        headers={
            "Title": title,
            "Click": pages_url,
            "Tags": "sparkles,shopping_bags",
        },
        timeout=20,
    )


if __name__ == "__main__":
    main()
