import itertools
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from metering_billing.models import (
    BillableMetric,
    Event,
    Invoice,
    PlanComponent,
    PlanVersion,
    PriceAdjustment,
    Subscription,
)
from metering_billing.utils import now_utc
from metering_billing.utils.enums import INVOICE_STATUS, PRICE_ADJUSTMENT_TYPE
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient


@pytest.fixture
def draft_invoice_test_common_setup(
    generate_org_and_api_key,
    add_users_to_org,
    api_client_with_api_key_auth,
    add_customers_to_org,
    add_product_to_org,
    add_plan_to_product,
    add_plan_version_to_plan,
):
    def do_draft_invoice_test_common_setup(*, auth_method):
        setup_dict = {}
        # set up organizations and api keys
        org, key = generate_org_and_api_key()
        org2, key2 = generate_org_and_api_key()
        setup_dict = {
            "org": org,
            "key": key,
            "org2": org2,
            "key2": key2,
        }
        # set up the client with the appropriate api key spec
        if auth_method == "api_key":
            client = api_client_with_api_key_auth(key)
        elif auth_method == "session_auth":
            client = APIClient()
            (user,) = add_users_to_org(org, n=1)
            client.force_authenticate(user=user)
            setup_dict["user"] = user
        else:
            client = api_client_with_api_key_auth(key)
            (user,) = add_users_to_org(org, n=1)
            client.force_authenticate(user=user)
            setup_dict["user"] = user
        setup_dict["client"] = client
        (customer,) = add_customers_to_org(org, n=1)
        setup_dict["customer"] = customer
        event_properties = (
            {"num_characters": 350, "peak_bandwith": 65},
            {"num_characters": 125, "peak_bandwith": 148},
            {"num_characters": 543, "peak_bandwith": 16},
        )
        event_set = baker.make(
            Event,
            organization=org,
            customer=customer,
            event_name="email_sent",
            time_created=now_utc() - timedelta(days=1),
            properties=itertools.cycle(event_properties),
            _quantity=3,
        )
        metric_set = baker.make(
            BillableMetric,
            organization=org,
            event_name="email_sent",
            property_name=itertools.cycle(["num_characters", "peak_bandwith", ""]),
            aggregation_type=itertools.cycle(["sum", "max", "count"]),
            _quantity=3,
        )
        setup_dict["metrics"] = metric_set
        product = add_product_to_org(org)
        plan = add_plan_to_product(product)
        plan_version = add_plan_version_to_plan(plan)
        plan_component_set = baker.make(
            PlanComponent,
            billable_metric=itertools.cycle(metric_set),
            free_metric_units=itertools.cycle([50, 0, 1]),
            cost_per_batch=itertools.cycle([5, 0.05, 2]),
            metric_units_per_batch=itertools.cycle([100, 1, 1]),
            _quantity=3,
        )
        setup_dict["plan_components"] = plan_component_set
        plan_version.components.add(*plan_component_set)
        plan_version.save()
        setup_dict["billing_plan"] = plan_version
        subscription = Subscription.objects.create(
            organization=org,
            customer=customer,
            billing_plan=plan_version,
            start_date=now_utc() - timedelta(days=3),
            status="active",
        )
        setup_dict["subscription"] = subscription

        return setup_dict

    return do_draft_invoice_test_common_setup


@pytest.mark.django_db(transaction=True)
class TestGenerateInvoice:
    def test_generate_invoice(self, draft_invoice_test_common_setup):
        setup_dict = draft_invoice_test_common_setup(auth_method="session_auth")

        active_subscriptions = Subscription.objects.filter(
            status="active",
            organization=setup_dict["org"],
            customer=setup_dict["customer"],
        )
        assert len(active_subscriptions) == 1

        prev_invoices_len = Invoice.objects.filter(
            payment_status=INVOICE_STATUS.DRAFT
        ).count()
        payload = {"customer_id": setup_dict["customer"].customer_id}
        response = setup_dict["client"].get(reverse("draft_invoice"), payload)

        assert response.status_code == status.HTTP_200_OK
        after_active_subscriptions = Subscription.objects.filter(
            status="active",
            organization=setup_dict["org"],
            customer=setup_dict["customer"],
        )
        assert len(after_active_subscriptions) == len(active_subscriptions)
        new_invoices_len = Invoice.objects.filter(
            payment_status=INVOICE_STATUS.DRAFT
        ).count()

        assert new_invoices_len == prev_invoices_len + 1

    def test_generate_invoice_with_price_adjustments(
        self, draft_invoice_test_common_setup
    ):
        setup_dict = draft_invoice_test_common_setup(auth_method="session_auth")

        active_subscriptions = Subscription.objects.filter(
            status="active",
            organization=setup_dict["org"],
            customer=setup_dict["customer"],
        )
        assert len(active_subscriptions) == 1

        payload = {"customer_id": setup_dict["customer"].customer_id}
        response = setup_dict["client"].get(reverse("draft_invoice"), payload)

        assert response.status_code == status.HTTP_200_OK
        before_cost = response.data[0]["cost_due"]

        pct_price_adjustment = PriceAdjustment.objects.create(
            organization=setup_dict["org"],
            price_adjustment_name=r"1% discount",
            price_adjustment_description=r"1% discount for being a valued customer",
            price_adjustment_type=PRICE_ADJUSTMENT_TYPE.PERCENTAGE,
            price_adjustment_amount=-1,
        )
        setup_dict["billing_plan"].price_adjustment = pct_price_adjustment
        setup_dict["billing_plan"].save()

        response = setup_dict["client"].get(reverse("draft_invoice"), payload)

        assert response.status_code == status.HTTP_200_OK
        after_cost = response.data[0]["cost_due"]
        assert (before_cost * Decimal("0.99")).quantize(Decimal(10) ** -2) == after_cost

        fixed_price_adjustment = PriceAdjustment.objects.create(
            organization=setup_dict["org"],
            price_adjustment_name=r"$1 discount",
            price_adjustment_description=r"$1 discount for being a valued customer",
            price_adjustment_type=PRICE_ADJUSTMENT_TYPE.FIXED,
            price_adjustment_amount=-1,
        )
        setup_dict["billing_plan"].price_adjustment = fixed_price_adjustment
        setup_dict["billing_plan"].save()

        response = setup_dict["client"].get(reverse("draft_invoice"), payload)

        assert response.status_code == status.HTTP_200_OK
        after_cost = response.data[0]["cost_due"]
        assert before_cost - Decimal("1") == after_cost

        override_price_adjustment = PriceAdjustment.objects.create(
            organization=setup_dict["org"],
            price_adjustment_name=r"$20 negoatiated price",
            price_adjustment_description=r"$20 price negotiated with sales team",
            price_adjustment_type=PRICE_ADJUSTMENT_TYPE.PRICE_OVERRIDE,
            price_adjustment_amount=20,
        )
        setup_dict["billing_plan"].price_adjustment = override_price_adjustment
        setup_dict["billing_plan"].save()

        response = setup_dict["client"].get(reverse("draft_invoice"), payload)

        assert response.status_code == status.HTTP_200_OK
        after_cost = response.data[0]["cost_due"]
        assert Decimal("20") == after_cost
