import { reactive } from "vue";
import type { FormRules } from "element-plus";
import { $t, transformI18n } from "@/plugins/i18n";

/**
 * 登录校验。
 *
 * 只校验“必填”：密码强度与格式由后端（M2a 的 JWT 登录）判定，
 * 前端不再复制基座的密码正则与验证码规则，避免把管理员种下的
 * 合法密码挡在表单里。
 */
const loginRules = reactive<FormRules>({
  username: [
    {
      validator: (rule, value, callback) => {
        if (value === "" || value === null || value === undefined) {
          callback(new Error(transformI18n($t("login.pureUsernameReg"))));
        } else {
          callback();
        }
      },
      trigger: "blur"
    }
  ],
  password: [
    {
      validator: (rule, value, callback) => {
        if (value === "") {
          callback(new Error(transformI18n($t("login.purePassWordReg"))));
        } else {
          callback();
        }
      },
      trigger: "blur"
    }
  ]
});

export { loginRules };
