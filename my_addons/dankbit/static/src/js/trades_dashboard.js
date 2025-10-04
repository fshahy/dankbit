/** @odoo-module **/

import { Component, useState, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class TradesDashboard extends Component {
    setup() {
        this.orm = useService("orm");
        this.state = useState({ data: {} });
        this.intervalId = null;

        // Load once before rendering
        onWillStart(async () => {
            await this.loadData();
        });

        // Start auto-refresh after mount
        onMounted(() => {
            this.intervalId = setInterval(async () => {
                await this.loadData();
            }, 1 * 60 * 1000); // 1 minutes
        });

        // Clean up when leaving the page
        onWillUnmount(() => {
            if (this.intervalId) {
                clearInterval(this.intervalId);
            }
        });
    }

    async loadData() {
        this.state.data = await this.orm.call(
            "dankbit.trade",          // your model
            "get_market_summary",     // your method
            []
        );
    }
}

TradesDashboard.template = "dankbit_dashboard.TradesDashboardTemplate";
registry.category("actions").add("dankbit.trades_dashboard", TradesDashboard);
