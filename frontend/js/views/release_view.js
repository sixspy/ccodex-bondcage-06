export const releaseViewTemplate = `
<section class="panel">
  <h2>解笼拨付</h2>
  <p class="hint">仅核验通过的笼可拨付。出金强制绑票：必须填写本笼核验票号（见履约核验页），缺票/废票一律拒写——拨付册零行、笼可用与账户冻结零改，只留拒写审计。默认把可用额全额拨给受益人，也可部分解笼。</p>
  <div class="grid-2">
    <div>
      <div class="form-row">
        <div>
          <label>通过态笼</label>
          <select v-model.number="releaseForm.cage_id">
            <option :value="0">请选择</option>
            <option v-for="c in passCages" :key="c.id" :value="c.id">
              {{ c.cage_code }} · 可用 {{ fmt(c.available) }}
            </option>
          </select>
        </div>
        <div>
          <label>核验票号（必填）</label>
          <input
            v-model="releaseForm.attest_ticket_no"
            list="attest-ticket-list"
            placeholder="ATT-笼码-g代-序号"
          />
          <datalist id="attest-ticket-list">
            <option
              v-for="a in passAttestations"
              :key="a.id"
              :value="a.ticket_no"
            >{{ a.cage_code }} · {{ a.ticket_no }}</option>
          </datalist>
        </div>
        <div>
          <label>金额（空=全额）</label>
          <input type="number" v-model.number="releaseForm.amount" min="0" step="1000" placeholder="全额"/>
        </div>
        <div>
          <label>收款方（空=受益人）</label>
          <input v-model="releaseForm.payee"/>
        </div>
      </div>
      <div class="actions">
        <button class="ok" @click="doRelease">绑票解笼拨付</button>
      </div>
    </div>
    <div>
      <table>
        <thead>
          <tr><th>凭证</th><th>核验票号</th><th>笼</th><th>金额</th><th>收款方</th><th>笼代</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in releases" :key="r.id">
            <td>{{ r.release_ref }}</td>
            <td>{{ r.attest_ticket_no || '—' }}</td>
            <td>{{ r.cage_code }}</td>
            <td>{{ fmt(r.amount) }}</td>
            <td>{{ r.payee }}</td>
            <td>{{ r.cage_generation }}</td>
            <td>{{ r.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</section>
`;
