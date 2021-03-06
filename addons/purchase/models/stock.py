# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    purchase_id = fields.Many2one('purchase.order', related='move_lines.purchase_line_id.order_id',
        string="Purchase Orders", readonly=True)


class StockMove(models.Model):
    _inherit = 'stock.move'

    purchase_line_id = fields.Many2one('purchase.order.line',
        'Purchase Order Line', ondelete='set null', index=True, readonly=True, copy=False)
    created_purchase_line_id = fields.Many2one('purchase.order.line',
        'Created Purchase Order Line', ondelete='set null', readonly=True, copy=False)

    @api.model
    def _prepare_merge_moves_distinct_fields(self):
        distinct_fields = super(StockMove, self)._prepare_merge_moves_distinct_fields()
        distinct_fields += ['purchase_line_id', 'created_purchase_line_id']
        return distinct_fields

    @api.model
    def _prepare_merge_move_sort_method(self, move):
        move.ensure_one()
        keys_sorted = super(StockMove, self)._prepare_merge_move_sort_method(move)
        keys_sorted += [move.purchase_line_id.id, move.created_purchase_line_id.id]
        return keys_sorted

    @api.multi
    def _get_price_unit(self):
        """ Returns the unit price for the move"""
        self.ensure_one()
        if self.purchase_line_id and self.product_id.id == self.purchase_line_id.product_id.id:
            line = self.purchase_line_id
            order = line.order_id
            price_unit = line.price_unit
            if line.taxes_id:
                price_unit = line.taxes_id.with_context(round=False).compute_all(price_unit, currency=line.order_id.currency_id, quantity=1.0)['total_excluded']
            if line.product_uom.id != line.product_id.uom_id.id:
                price_unit *= line.product_uom.factor / line.product_id.uom_id.factor
            if order.currency_id != order.company_id.currency_id:
                price_unit = order.currency_id._convert(
                    price_unit, order.company_id.currency_id, order.company_id, order.date_order or fields.Date.today(), round=False)
            return price_unit
        return super(StockMove, self)._get_price_unit()

    def _generate_valuation_lines_data(self, partner_id, qty, debit_value, credit_value, debit_account_id, credit_account_id):
        """ Overridden from stock_account to support amount_currency on valuation lines generated from po
        """
        self.ensure_one()

        rslt = super(StockMove, self)._generate_valuation_lines_data(partner_id, qty, debit_value, credit_value, debit_account_id, credit_account_id)

        if self.purchase_line_id:
            purchase_currency = self.purchase_line_id.currency_id
            if purchase_currency != self.company_id.currency_id:
                purchase_price_unit = self.purchase_line_id.price_unit
                currency_move_valuation = purchase_currency.round(purchase_price_unit * qty)
                rslt['credit_line_vals']['amount_currency'] = rslt['credit_line_vals']['credit'] and -currency_move_valuation or currency_move_valuation
                rslt['credit_line_vals']['currency_id'] = purchase_currency.id
                rslt['debit_line_vals']['amount_currency'] = rslt['debit_line_vals']['credit'] and -currency_move_valuation or currency_move_valuation
                rslt['debit_line_vals']['currency_id'] = purchase_currency.id
        return rslt

    def _prepare_extra_move_vals(self, qty):
        vals = super(StockMove, self)._prepare_extra_move_vals(qty)
        vals['purchase_line_id'] = self.purchase_line_id.id
        return vals

    def _prepare_move_split_vals(self, uom_qty):
        vals = super(StockMove, self)._prepare_move_split_vals(uom_qty)
        vals['purchase_line_id'] = self.purchase_line_id.id
        return vals

    def _action_done(self):
        res = super(StockMove, self)._action_done()
        self.mapped('purchase_line_id').sudo()._update_received_qty()
        return res

    def write(self, vals):
        res = super(StockMove, self).write(vals)
        if 'product_uom_qty' in vals:
            self.filtered(lambda m: m.state == 'done' and m.purchase_line_id).mapped(
                'purchase_line_id').sudo()._update_received_qty()
        return res

    def _get_upstream_documents_and_responsibles(self, visited):
        if self.created_purchase_line_id and self.created_purchase_line_id.state not in ('done', 'cancel'):
            return [(self.created_purchase_line_id.order_id, self.created_purchase_line_id.order_id.user_id, visited)]
        else:
            return super(StockMove, self)._get_upstream_documents_and_responsibles(visited)

    def _get_related_invoices(self):
        """ Overridden to return the vendor bills related to this stock move.
        """
        rslt = super(StockMove, self)._get_related_invoices()
        rslt += self.mapped('picking_id.purchase_id.invoice_ids').filtered(lambda x: x.state not in ('draft', 'cancel'))
        return rslt


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    buy_to_resupply = fields.Boolean('Purchase to resupply this warehouse', default=True,
                                     help="When products are bought, they can be delivered to this warehouse")
    buy_pull_id = fields.Many2one('procurement.rule', 'Buy rule')

    @api.multi
    def _get_buy_pull_rule(self):
        try:
            buy_route_id = self.env['ir.model.data'].get_object_reference('purchase', 'route_warehouse0_buy')[1]
        except:
            buy_route_id = self.env['stock.location.route'].search([('name', 'like', _('Buy'))])
            buy_route_id = buy_route_id[0].id if buy_route_id else False
        if not buy_route_id:
            raise UserError(_("Can't find any generic Buy route."))

        return {
            'name': self._format_routename(_(' Buy')),
            'location_id': self.in_type_id.default_location_dest_id.id,
            'route_id': buy_route_id,
            'action': 'buy',
            'picking_type_id': self.in_type_id.id,
            'warehouse_id': self.id,
            'group_propagation_option': 'none',
        }

    @api.multi
    def create_routes(self):
        res = super(StockWarehouse, self).create_routes() # super applies ensure_one()
        if self.buy_to_resupply:
            buy_pull_vals = self._get_buy_pull_rule()
            buy_pull = self.env['procurement.rule'].create(buy_pull_vals)
            res['buy_pull_id'] = buy_pull.id
        return res

    @api.multi
    def write(self, vals):
        if 'buy_to_resupply' in vals:
            if vals.get("buy_to_resupply"):
                for warehouse in self:
                    if not warehouse.buy_pull_id:
                        buy_pull_vals = self._get_buy_pull_rule()
                        buy_pull = self.env['procurement.rule'].create(buy_pull_vals)
                        vals['buy_pull_id'] = buy_pull.id
            else:
                for warehouse in self:
                    if warehouse.buy_pull_id:
                        warehouse.buy_pull_id.unlink()
        return super(StockWarehouse, self).write(vals)

    @api.multi
    def _get_all_routes(self):
        routes = super(StockWarehouse, self).get_all_routes_for_wh()
        routes |= self.filtered(lambda self: self.buy_to_resupply and self.buy_pull_id and self.buy_pull_id.route_id).mapped('buy_pull_id').mapped('route_id')
        return routes

    @api.multi
    def _update_name_and_code(self, name=False, code=False):
        res = super(StockWarehouse, self)._update_name_and_code(name, code)
        warehouse = self[0]
        #change the buy procurement rule name
        if warehouse.buy_pull_id and name:
            warehouse.buy_pull_id.write({'name': warehouse.buy_pull_id.name.replace(warehouse.name, name, 1)})
        return res

    @api.multi
    def _update_routes(self):
        res = super(StockWarehouse, self)._update_routes()
        for warehouse in self:
            if warehouse.in_type_id.default_location_dest_id != warehouse.buy_pull_id.location_id:
                warehouse.buy_pull_id.write({'location_id': warehouse.in_type_id.default_location_dest_id.id})
        return res

class ReturnPicking(models.TransientModel):
    _inherit = "stock.return.picking"

    def _prepare_move_default_values(self, return_line, new_picking):
        vals = super(ReturnPicking, self)._prepare_move_default_values(return_line, new_picking)
        vals['purchase_line_id'] = return_line.move_id.purchase_line_id.id
        return vals


class Orderpoint(models.Model):
    _inherit = "stock.warehouse.orderpoint"

    def _quantity_in_progress(self):
        res = super(Orderpoint, self)._quantity_in_progress()
        for poline in self.env['purchase.order.line'].search([('state','in',('draft','sent','to approve')),('orderpoint_id','in',self.ids)]):
            res[poline.orderpoint_id.id] += poline.product_uom._compute_quantity(poline.product_qty, poline.orderpoint_id.product_uom, round=False)
        return res

    def action_view_purchase(self):
        """ This function returns an action that display existing
        purchase orders of given orderpoint.
        """
        action = self.env.ref('purchase.purchase_rfq')
        result = action.read()[0]

        # Remvove the context since the action basically display RFQ and not PO.
        result['context'] = {}
        order_line_ids = self.env['purchase.order.line'].search([('orderpoint_id', '=', self.id)])
        purchase_ids = order_line_ids.mapped('order_id')

        result['domain'] = "[('id','in',%s)]" % (purchase_ids.ids)

        return result
