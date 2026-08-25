import os
import base64
import json
import re
import time

import requests
from flask import Flask, render_template, request, jsonify
from openai import OpenAI

app = Flask(__name__)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

DATAFORSEO_LOGIN = os.environ.get("DATAFORSEO_LOGIN")
DATAFORSEO_PASSWORD = os.environ.get("DATAFORSEO_PASSWORD")


# Australia
LOCATION_CODE = 2036

DATAFORSEO_POST_URL = "https://api.dataforseo.com/v3/serp/google/organic/task_post"

DATAFORSEO_GET_URL = (
    "https://api.dataforseo.com/v3/serp/google/organic/task_get/advanced/{}"
)


@app.route("/")
def index():
    return render_template("index.html")


# ============================================================
# PRODUCT IDENTIFICATION
# ============================================================


def identify_product(image_file):

    image_bytes = image_file.read()

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    mime_type = image_file.mimetype or "image/jpeg"

    prompt = """
Identify the packaged food product in this photograph.

Return ONLY valid JSON in this format:

{
    "brand": "",
    "product_name": "",
    "size": "",
    "barcode": null,
    "search_query": "",
    "confidence": 0.0
}

Instructions:

- Identify the exact product if possible.
- Include the product size/weight if visible.
- If a barcode is clearly visible and readable, return it.
- Do NOT invent a barcode.
- search_query should be a concise Google search query that uniquely
  identifies the product.
- confidence should be between 0 and 1.
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You identify packaged food products from photographs. "
                    "Be conservative and do not invent product information."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": (f"data:{mime_type};base64,{image_b64}"),
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
    )

    result = json.loads(response.choices[0].message.content)

    return result


# ============================================================
# DATAFORSEO
# ============================================================


def create_dataforseo_task(search_query):

    payload = [
        {
            "language_code": "en",
            "location_code": LOCATION_CODE,
            # Quotes make the search much more product-specific
            "keyword": f'"{search_query}"',
            "device": "desktop",
            "os": "macos",
            # Higher priority
            "priority": 2,
        }
    ]

    response = requests.post(
        DATAFORSEO_POST_URL,
        json=payload,
        auth=(DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD),
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    if data.get("status_code") != 20000:
        raise Exception(data.get("status_message", "DataForSEO request failed"))

    tasks = data.get("tasks", [])

    if not tasks:
        raise Exception("DataForSEO returned no task.")

    task = tasks[0]

    if task.get("status_code") != 20100:
        raise Exception(task.get("status_message", "DataForSEO task creation failed"))

    return task["id"]


def get_dataforseo_task(task_id):

    url = DATAFORSEO_GET_URL.format(task_id)

    response = requests.get(
        url, auth=(DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD), timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if data.get("status_code") != 20000:
        raise Exception(data.get("status_message", "DataForSEO request failed"))

    tasks = data.get("tasks", [])

    if not tasks:
        return None

    task = tasks[0]

    # Not finished yet
    if task.get("status_code") != 20000:
        return None

    results = task.get("result")

    if not results:
        return None

    return results[0]


def wait_for_dataforseo(task_id):

    # Poll for up to 30 seconds
    for i in range(15):

        result = get_dataforseo_task(task_id)

        if result:
            return result

        time.sleep(2)

    raise Exception("Timed out waiting for Google search result.")


# ============================================================
# EXTRACT GOOGLE PRODUCT RATING
# ============================================================


def extract_rating(data):
    """
    Search a DataForSEO SERP response for the best product rating.
    """

    ratings = []

    try:
        tasks = data.get("tasks", [])

        for task in tasks:

            results = task.get("result") or []

            for result in results:

                items = result.get("items", [])

                # ------------------------------------------------
                # Normal SERP items
                # ------------------------------------------------

                for item in items:

                    rating = item.get("rating")

                    if rating and rating.get("value") is not None:
                        ratings.append(
                            {
                                "rating": float(rating["value"]),
                                "votes": rating.get("votes_count", 0),
                                "title": item.get("title"),
                                "source": item.get("domain"),
                            }
                        )

                    # ------------------------------------------------
                    # Popular products contains another items array
                    # ------------------------------------------------

                    for product in item.get("items", []):

                        rating = product.get("rating")

                        if rating and rating.get("value") is not None:

                            ratings.append(
                                {
                                    "rating": float(rating["value"]),
                                    "votes": rating.get("votes_count", 0),
                                    "title": product.get("title"),
                                    "source": product.get("seller"),
                                }
                            )

        if not ratings:
            return None

        # Prefer ratings with more votes.
        # This avoids selecting something like a 5.0 rating
        # based on a single review when a 4.8 rating has 61 reviews.
        ratings.sort(key=lambda x: (x["votes"], x["rating"]), reverse=True)

        return ratings[0]

    except Exception as e:

        print("Rating extraction error:", e)

        return None


# ============================================================
# FIND GOOGLE PRODUCT INFORMATION
# ============================================================


def extract_product_info(serp_result):

    items = serp_result.get("items", [])

    for item in items:

        if item.get("type") == "knowledge_graph":

            return {"title": item.get("title"), "description": item.get("description")}

    return {}


# ============================================================
# MAIN SCAN ENDPOINT
# ============================================================


@app.route("/scan", methods=["POST"])
def scan():

    # ============================================================
    # CHECK PHOTO
    # ============================================================

    if "photo" not in request.files:
        return jsonify({
            "error": "No photo uploaded."
        }), 400

    photo = request.files["photo"]

    if photo.filename == "":
        return jsonify({
            "error": "No photo selected."
        }), 400


    # ============================================================
    # READ PHOTO
    # ============================================================

    try:

        image_bytes = photo.read()

        image_base64 = base64.b64encode(
            image_bytes
        ).decode("utf-8")

    except Exception as e:

        print("Photo error:", e)

        return jsonify({
            "error": "Could not read the photo."
        }), 500


    # ============================================================
    # IDENTIFY PRODUCT WITH OPENAI
    # ============================================================

    try:

        response = client.responses.create(

            model="gpt-5",

            input=[
                {
                    "role": "user",

                    "content": [

                        {
                            "type": "input_text",

                            "text": """
Identify the exact consumer product shown in this image.

Return ONLY valid JSON in exactly this format:

{
    "brand": "...",
    "product_name": "...",
    "variant": "...",
    "size": "..."
}

Instructions:

- brand = the manufacturer or brand shown on the packaging.
- product_name = the generic product name, excluding brand and variant.
- variant = flavour, colour, formulation, model, style, scent,
  strength, version or other distinguishing descriptor.
- size = package size if visible.
- Do not invent information.
- If a field is not reasonably clear, return an empty string.
- Be as specific as the packaging allows.

The combination of brand + product_name + variant + size
should identify the specific product rather than merely the
general product category.
"""
                        },

                        {
                            "type": "input_image",

                            "image_url":
                                f"data:{photo.mimetype};base64,{image_base64}"
                        }

                    ]
                }
            ]
        )


        product_text = response.output_text

        print("OpenAI product response:")
        print(product_text)


        product = json.loads(product_text)


        brand = (
            product.get("brand") or ""
        ).strip()

        product_name = (
            product.get("product_name") or ""
        ).strip()

        variant = (
            product.get("variant") or ""
        ).strip()

        size = (
            product.get("size") or ""
        ).strip()


        if not product_name:

            return jsonify({
                "error": "Could not identify the product."
            }), 400


    except Exception as e:

        print("OpenAI error:", e)

        return jsonify({
            "error": "Could not identify the product."
        }), 500


    # ============================================================
    # BUILD SPECIFIC SEARCH QUERY
    # ============================================================

    search_parts = []

    if brand:
        search_parts.append(brand)

    if product_name:
        search_parts.append(product_name)

    if variant:
        search_parts.append(variant)

    if size:
        search_parts.append(size)


    search_query = " ".join(search_parts)


    print("DataForSEO search:")
    print(search_query)


    # ============================================================
    # CREATE DATAFORSEO TASK
    # ============================================================

    try:

        payload = [
            {
                "language_code": "en",
                "location_code": 2036,
                "keyword": search_query,
                "device": "desktop",
                "os": "macos",
                "priority": 2
            }
        ]


        response = requests.post(

            "https://api.dataforseo.com/v3/serp/google/organic/task_post",

            auth=(
                DATAFORSEO_LOGIN,
                DATAFORSEO_PASSWORD
            ),

            json=payload,

            timeout=30
        )


        response.raise_for_status()

        data = response.json()

        print("DataForSEO task response:")
        print(data)


        if data.get("status_code") != 20000:

            return jsonify({
                "error": "DataForSEO task creation failed.",
                "details": data
            }), 500


        tasks = data.get("tasks") or []


        if not tasks:

            return jsonify({
                "error": "DataForSEO returned no task."
            }), 500


        task_id = tasks[0].get("id")


        if not task_id:

            return jsonify({
                "error": "DataForSEO did not return a task ID."
            }), 500


    except Exception as e:

        print("DataForSEO task error:", e)

        return jsonify({
            "error": "Could not search for product reviews."
        }), 500


    # ============================================================
    # WAIT FOR DATAFORSEO
    # ============================================================

    import time

    data = None


    for attempt in range(20):

        time.sleep(1)


        try:

            response = requests.get(

                "https://api.dataforseo.com/v3/serp/google/organic/"
                f"task_get/advanced/{task_id}",

                auth=(
                    DATAFORSEO_LOGIN,
                    DATAFORSEO_PASSWORD
                ),

                timeout=30
            )


            response.raise_for_status()

            result = response.json()


            print(
                f"DataForSEO retrieve attempt {attempt + 1}:"
            )

            print(result)


            tasks = result.get("tasks") or []


            if not tasks:
                continue


            task = tasks[0]


            if task.get("status_code") != 20000:
                continue


            if task.get("result"):

                data = result

                break


        except Exception as e:

            print(
                "DataForSEO retrieve error:",
                e
            )


    # ============================================================
    # COLLECT RATED CANDIDATES
    # ============================================================

    candidates = []


    if data:

        try:

            tasks = data.get("tasks") or []


            for task in tasks:

                results = task.get("result") or []


                for result in results:

                    items = result.get("items") or []


                    for item in items:

                        # ------------------------------------------------
                        # Normal SERP result
                        # ------------------------------------------------

                        rating = item.get("rating")


                        if (
                            isinstance(rating, dict)
                            and rating.get("value") is not None
                        ):

                            candidates.append({

                                "title":
                                    item.get("title") or "",

                                "rating":
                                    float(
                                        rating.get("value")
                                    ),

                                "votes":
                                    int(
                                        rating.get(
                                            "votes_count",
                                            0
                                        ) or 0
                                    ),

                                "source":
                                    item.get("domain"),

                                "type":
                                    item.get("type")

                            })


                        # ------------------------------------------------
                        # Nested products
                        # ------------------------------------------------

                        nested_items = (
                            item.get("items") or []
                        )


                        if not isinstance(
                            nested_items,
                            list
                        ):
                            continue


                        for product_item in nested_items:

                            if not isinstance(
                                product_item,
                                dict
                            ):
                                continue


                            rating = product_item.get(
                                "rating"
                            )


                            if (
                                isinstance(
                                    rating,
                                    dict
                                )
                                and
                                rating.get("value")
                                is not None
                            ):

                                candidates.append({

                                    "title":
                                        product_item.get(
                                            "title"
                                        ) or "",

                                    "rating":
                                        float(
                                            rating.get(
                                                "value"
                                            )
                                        ),

                                    "votes":
                                        int(
                                            rating.get(
                                                "votes_count",
                                                0
                                            ) or 0
                                        ),

                                    "source":
                                        product_item.get(
                                            "seller"
                                        ),

                                    "type":
                                        "nested_product"

                                })


        except Exception as e:

            print(
                "Rating extraction error:",
                e
            )


    print(
        f"DataForSEO returned "
        f"{len(candidates)} rated candidates."
    )


    # ============================================================
    # GENERIC PRODUCT MATCHING
    # ============================================================

    def normalise(text):

        import re

        text = str(text or "").lower()

        text = re.sub(
            r"[^a-z0-9]+",
            " ",
            text
        )

        return text.strip()


    def tokens(text):

        stop_words = {

            "the",
            "and",
            "with",
            "for",
            "from",
            "pack",
            "of",
            "x",
            "size"

        }

        return {
            word
            for word in normalise(text).split()
            if len(word) > 1
            and word not in stop_words
        }


    brand_tokens = tokens(brand)
    name_tokens = tokens(product_name)
    variant_tokens = tokens(variant)
    size_tokens = tokens(size)


    # These are deliberately generic.
    #
    # Variant/product descriptors are more useful for identifying
    # the exact item than generic words such as "chips", "shampoo",
    # "coffee", etc.
    #
    # There are NO product-category-specific words here.

    def candidate_score(candidate):

        title = normalise(
            candidate.get("title")
        )

        title_tokens = tokens(title)


        # --------------------------------------------
        # Brand match
        # --------------------------------------------

        brand_matches = (
            len(
                brand_tokens &
                title_tokens
            )
        )

        brand_score = (
            brand_matches /
            len(brand_tokens)
            if brand_tokens
            else 0
        )


        # --------------------------------------------
        # Product-name match
        # --------------------------------------------

        name_matches = (
            len(
                name_tokens &
                title_tokens
            )
        )

        name_score = (
            name_matches /
            len(name_tokens)
            if name_tokens
            else 0
        )


        # --------------------------------------------
        # Variant match
        # --------------------------------------------

        variant_matches = (
            len(
                variant_tokens &
                title_tokens
            )
        )

        variant_score = (
            variant_matches /
            len(variant_tokens)
            if variant_tokens
            else 0
        )


        # --------------------------------------------
        # Size match
        # --------------------------------------------

        size_matches = (
            len(
                size_tokens &
                title_tokens
            )
        )

        size_score = (
            size_matches /
            len(size_tokens)
            if size_tokens
            else 0
        )


        # --------------------------------------------
        # Overall score
        #
        # Exact product identity should dominate.
        # Variant is particularly important because two
        # products can have identical brand/product names
        # but different variants.
        # --------------------------------------------

        score = (

            brand_score * 0.25 +

            name_score * 0.35 +

            variant_score * 0.30 +

            size_score * 0.10

        )


        # --------------------------------------------
        # Small bonus for containing the full search
        # phrase approximately.
        # --------------------------------------------

        search_tokens = (
            brand_tokens |
            name_tokens |
            variant_tokens
        )


        if search_tokens:

            overall_match = (
                len(
                    search_tokens &
                    title_tokens
                )
                /
                len(search_tokens)
            )

            score += (
                overall_match * 0.10
            )


        return min(score, 1.0)


    # Score candidates

    for candidate in candidates:

        candidate["score"] = candidate_score(
            candidate
        )


    # Sort by product match first,
    # then votes, then rating.

    candidates.sort(

        key=lambda x: (

            x.get("score", 0),

            x.get("votes", 0),

            x.get("rating", 0)

        ),

        reverse=True

    )


    # ============================================================
    # PRINT MATCHING CANDIDATES
    # ============================================================

    print("")
    print("Matching product candidates:")


    for candidate in candidates[:20]:

        print(

            f"  {candidate.get('title')} | "
            f"{candidate.get('rating')} stars | "
            f"{candidate.get('votes')} votes | "
            f"score={candidate.get('score', 0):.3f}"

        )


    # ============================================================
    # SELECT PRODUCT
    # ============================================================

    best_candidate = None


    if candidates:

        best_candidate = candidates[0]
        # ============================================================
        # TOP 3 SIMILAR PRODUCTS
        # ============================================================

        similar_products = []

        for candidate in candidates[1:]:

            # Don't show candidates with no meaningful title
            if not candidate.get("title"):
                continue

            similar_products.append({

                "title":
                    candidate.get("title"),

                "rating":
                    candidate.get("rating"),

                "review_count":
                    candidate.get("votes", 0),

                "source":
                    candidate.get("source")

            })


        # Rank similar products by rating, then review count

        similar_products.sort(

            key=lambda x: (

                x.get("rating") or 0,

                x.get("review_count") or 0

            ),

            reverse=True

        )


        similar_products = similar_products[:3]

        print("")
        print("Selected product:")
        print(
            f"  {best_candidate.get('title')}"
        )
        print(
            f"  Rating: "
            f"{best_candidate.get('rating')}"
        )
        print(
            f"  Votes: "
            f"{best_candidate.get('votes')}"
        )
        print(
            f"  Score: "
            f"{best_candidate.get('score', 0):.2f}"
        )


    # ============================================================
    # NO RATING FOUND
    # ============================================================

    if not best_candidate:

        return jsonify({

            "product": {
                "brand": brand,
                "product_name": product_name,
                "variant": variant,
                "size": size
            },

            "search_query": search_query,

            "rating": None,

            "message":
                "Product identified, but no rating was found."

        })


    # ============================================================
    # RETURN RESULT
    # ============================================================

    return jsonify({

    "product": {

        "brand": brand,

        "product_name": product_name,

        "variant": variant,

        "size": size

    },

    "search_query": search_query,

    "rating": {

        "rating":
            best_candidate.get("rating"),

        "review_count":
            best_candidate.get("votes", 0),

        "source":
            best_candidate.get("source"),

        "title":
            best_candidate.get("title"),

        "match_score":
            best_candidate.get("score", 0)

    },

    "similar_products":
        similar_products,

    "message":
        "Rating found."

})


if __name__ == "__main__":

    app.run(host="0.0.0.0", port=5001, debug=True)
