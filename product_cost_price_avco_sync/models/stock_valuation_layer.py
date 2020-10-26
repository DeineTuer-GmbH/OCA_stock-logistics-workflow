# Copyright 2020 Tecnativa - Carlos Dauden
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import models
from odoo.tools import float_compare


class StockValuationLayer(models.Model):
    """Stock Valuation Layer"""

    _inherit = "stock.valuation.layer"

    def write(self, vals):
        """ Update cost price avco """
        if ("unit_cost" in vals or "quantity" in vals) and not self.env.context.get(
            "skip_avco_sync"
        ):
            self.cost_price_avco_sync(vals)
        return super().write(vals)

    def get_svls_to_avco_sync(self):
        self.ensure_one()
        domain = [
            ("company_id", "=", self.company_id.id),
            ("product_id", "=", self.product_id.id),
            ("create_date", ">=", self.create_date),
        ]
        return (
            self.env["stock.valuation.layer"]
            .sudo()
            .search(domain, order="create_date, id")
        )

    def cost_price_avco_sync(self, vals):
        procesed_lines = set()
        for line in self.sorted(key=lambda l: (l.create_date, l.id)):
            if (
                line.id in procesed_lines
                or line.product_id.cost_method != "average"
                or line.quantity <= 0.0
            ):
                continue
            previous_price = previous_qty = old_price = 0.0
            # update_enabled determines if svl is after modified line to enable
            # write changes
            update_enabled = False

            for svl in line.with_context(skip_avco_sync=True).get_svls_to_avco_sync():
                # Enable update mode for after lines
                if not update_enabled and svl.id == line.id:
                    update_enabled = True
                    qty = vals.get("quantity", line.quantity)
                else:
                    qty = svl.quantity

                f_compare = float_compare(
                    qty, 0.0, precision_rounding=svl.uom_id.rounding
                )
                # Incoming line in layer
                if f_compare > 0:
                    total_qty = previous_qty + qty
                    previous_price = line.currency_id.round(
                        (
                            (previous_price * previous_qty + svl.unit_cost * qty)
                            / total_qty
                        )
                        if total_qty
                        else svl.unit_cost
                    )
                    # Return moves
                    if svl.stock_move_id.move_orig_ids:
                        old_price = (
                            svl.stock_move_id.move_orig_ids[:1]
                            .stock_valuation_layer_ids[:1]
                            .unit_cost
                        )
                        svl.with_context(skip_avco_sync=True).write(
                            {
                                "unit_cost": line.currency_id.round(old_price),
                                "value": line.currency_id.round(
                                    old_price * svl.quantity
                                ),
                            }
                        )
                    # Normal incoming moves
                    else:
                        old_price = line.currency_id.round(
                            (
                                (old_price * previous_qty + svl.unit_cost * qty)
                                / total_qty
                            )
                            if total_qty
                            else svl.unit_cost
                        )
                    previous_qty = total_qty
                # Outgoing line in layer
                elif f_compare < 0:
                    if update_enabled and float_compare(
                        svl.unit_cost,
                        previous_price,
                        precision_rounding=svl.uom_id.rounding,
                    ):
                        vals_out = {
                            "unit_cost": line.currency_id.round(previous_price),
                            "value": line.currency_id.round(previous_price * qty),
                        }
                        svl.with_context(skip_avco_sync=True).write(vals_out)
                        # Update svl value from returned moves linked to this one
                        move_dest = svl.stock_move_id.move_dest_ids
                        if move_dest.location_id.usage == "customer":
                            move_dest.stock_valuation_layer_ids.with_context(
                                skip_avco_sync=True
                            ).write(vals_out)
                            procesed_lines.update(move_dest.stock_valuation_layer_ids.ids)
                    previous_qty += qty
                # Manual price adjustment line in layer
                elif not svl.unit_cost and not qty:
                    old_diff = svl.value / previous_qty
                    price = old_price + old_diff
                    if update_enabled:
                        new_diff = price - previous_price
                        new_value = line.currency_id.round(new_diff * previous_qty)
                        svl.with_context(skip_avco_sync=True).value = new_value
                        previous_price = price
                        break
                    # TODO: Avoid duplicate line keeping break and
                    #  previous_price updated
                    previous_price = price
                procesed_lines.add(svl.id)
            # Update product standard price if it is modified
            if float_compare(
                previous_price,
                line.product_id.with_context(
                    force_company=line.company_id.id
                ).standard_price,
                precision_rounding=line.currency_id.rounding,
            ):
                line.product_id.with_context(
                    force_company=line.company_id.id
                ).sudo().write({"standard_price": previous_price})
            # Compute new values for layer line
            vals.update(
                {
                    "value": line.currency_id.round(
                        vals["unit_cost"] * vals.get("quantity", line.quantity)
                    ),
                    "remaining_value": line.currency_id.round(
                        vals["unit_cost"] * line.remaining_qty
                    ),
                }
            )
            # Update unit_cost for incoming stock moves
            if line.stock_move_id:
                line.stock_move_id.price_unit = vals["unit_cost"]
